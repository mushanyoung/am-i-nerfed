"""Allowlisted public summaries; never copy arbitrary private report fields."""
import html
import json
import re
from pathlib import Path

from . import __version__

LIMITATION = (
    "Provider-reported metadata, not proof of model weights or intelligence. "
    "A short probe cannot establish account-wide or workload-independent behavior."
)
ROUTE_STATES = {"MATCH", "CHANGED", "UNKNOWN"}
EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}
EFFORT_STATES = {"MATCH", "CHANGED", "NOT_REPORTED", "NOT_REQUESTED"}
BACKENDS = {"claude-proxy", "claude-direct", "codex-cli", "codex-http"}


def label(value):
    """Reject paths, controls, email addresses, credentials and oversized labels."""
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:\[\]-]{0,119}", value):
        return "[redacted]"
    if re.search(r"(?:sk-|gh[pousr]_|github_pat_|Bearer|eyJ)", value, re.I):
        return "[redacted]"
    return value


def labels(values):
    if not isinstance(values, list):
        return []
    return list(dict.fromkeys(label(v) for v in values if isinstance(v, str)))


def effort_value(value):
    return value if isinstance(value, str) and value in EFFORTS else None


def effort_status(requested, reported):
    if not requested:
        return "NOT_REQUESTED"
    if not reported:
        return "NOT_REPORTED"
    return "MATCH" if requested == reported else "CHANGED"


def complete_evidence(record):
    return record.get("complete") is True and (
        "direct_control" not in record or record["direct_control"].get("complete") is True)


def same_model(requested, reported):
    if requested.startswith("claude-"):
        requested = re.sub(r"\[(?:1m|200k)\]$", "", requested)
    if requested == reported:
        return True
    base = requested[:-7] if requested.endswith("-latest") else requested
    return reported == base or bool(re.fullmatch(re.escape(base) + r"-(?:\d{8}|\d{4}-\d{2}-\d{2})", reported))


def row(provider, requested, wire, reported, route, effort_req=None,
        effort_reported=None, evidence="unknown", complete=False,
        wire_effort=None, reported_efforts=None, wire_efforts=None, backend=None):
    provider = provider if provider in ("claude", "codex") else "unknown"
    effort_req, effort_reported = effort_value(effort_req), effort_value(effort_reported)
    wire_effort = effort_value(wire_effort)
    observed_efforts = list(dict.fromkeys(e for e in (reported_efforts or [])
                                         if effort_value(e)))
    sent_efforts = list(dict.fromkeys(e for e in (wire_efforts or []) if effort_value(e)))
    if wire_effort and wire_effort not in sent_efforts:
        sent_efforts.append(wire_effort)
    if effort_reported and effort_reported not in observed_efforts:
        observed_efforts.append(effort_reported)
    e_status = effort_status(effort_req, effort_reported)
    if effort_req and (any(e != effort_req for e in sent_efforts)
                       or any(e != effort_req for e in observed_efforts)):
        e_status = "CHANGED"
    inferred_backend = {("claude", "upstream-http"): "claude-proxy",
                        ("claude", "cli-response"): "claude-direct",
                        ("codex", "upstream-http"): "codex-http",
                        ("codex", "cli-trace"): "codex-cli"}.get((provider, evidence if isinstance(evidence, str) else "unknown"))
    backend = inferred_backend or (backend if isinstance(backend, str) and backend in BACKENDS and backend.startswith(provider + "-") else "unknown")
    return {
        "provider": provider if provider in ("claude", "codex") else "unknown",
        "backend": backend,
        "requested_model": label(requested), "wire_models": labels(wire),
        "reported_models": labels(reported),
        "route_status": route if route in ROUTE_STATES else "UNKNOWN",
        "requested_effort": effort_req, "reported_effort": effort_reported,
        "wire_effort": wire_effort, "wire_efforts": sent_efforts, "reported_efforts": observed_efforts,
        "effort_status": e_status,
        "evidence": evidence if evidence in (
            "upstream-http", "cli-trace", "cli-response", "synthetic") else "unknown",
        "complete": complete is True,
    }


def _claude_rows(data):
    rows = []
    for result in data["results"]:
        captures = [c for c in result.get("captures", []) if c.get("is_probe_prompt")]
        wire = list(dict.fromkeys(c.get("request", {}).get("model") for c in captures))
        reported = list(dict.fromkeys(m for c in captures for m in c.get("response_models", [])))
        verdict = result.get("verdict", "UNKNOWN")
        completed = bool(captures) and all(c.get("complete") and c.get("http_status") == 200
                                          and not c.get("errors") and not c.get("transport_error")
                                          and not c.get("parse_errors") for c in captures)
        route = "UNKNOWN"
        if verdict in ("DIFFERENT", "CLIENT_MODEL_CHANGED", "REROUTED", "FALLBACK", "CLI_RESPONSE_DIFFERENT"):
            route = "CHANGED"
        elif verdict.startswith("MATCH") and completed and reported:
            route = "MATCH"
        efforts = [c.get("request", {}).get("output_config", {}).get("effort") for c in captures]
        efforts = [x for x in efforts if x]
        r = row("claude", result.get("requested"), wire, reported, route,
                data.get("effort_override") or (efforts[0] if efforts else None),
                evidence="upstream-http", complete=completed,
                wire_effort=efforts[0] if efforts else None, wire_efforts=efforts)
        control = result.get("direct_control")
        if control:
            control_cli = control.get("cli", {})
            r["direct_control"] = {
                "route_status": {"DIRECT_METADATA_MATCH": "MATCH", "DIRECT_METADATA_DIFFERENT": "CHANGED"}.get(control.get("verdict"), "UNKNOWN"),
                "reported_models": labels(control_cli.get("assistant_models", [])),
                "agrees": result.get("direct_control_agrees") is True,
                "complete": (control_cli.get("result_is_error") is False and control.get("cli_exit_code") == 0
                             and not control.get("timed_out") and not control_cli.get("parse_errors")
                             and not control_cli.get("errors") and control_cli.get("result_subtype") == "success"
                             and control.get("verdict") in ("DIRECT_METADATA_MATCH", "DIRECT_METADATA_DIFFERENT")),
            }
        rows.append(r)
    return rows


def _codex_rows(data):
    # Import here: the public exporter needs no credentials or auth reads.
    from .providers.codex import verdict_of
    rows = []
    for result in data["results"]:
        verdict = result.get("route_verdict") or result.get("verdict") or verdict_of(result)
        model_signals = (result.get("models_seen") or []) + [result.get(k) for k in ("header_model", "created_model", "completed_model")]
        complete = (result.get("completion_seen") is True and result.get("status") == 200
                    and not result.get("parse_errors") and result.get("final_status") == "completed"
                    and not result.get("error"))
        if result.get("verdict") == "REQUEST CHANGED" or verdict in ("DIFFERENT", "REROUTED", "CLIENT_MODEL_CHANGED", "CHANGED"):
            route = "CHANGED"
        elif verdict in ("same", "same(snapshot)", "MATCH", "MATCH_SNAPSHOT") and complete:
            route = "MATCH"
        else:
            route = "UNKNOWN"
        rows.append(row("codex", result.get("requested"), (result.get("wire_models") or []) + [result.get("wire_model")],
                        [m for m in model_signals if m], route,
                        result.get("effort"), result.get("served_effort"),
                        "cli-trace" if result.get("path") == "codex" else "upstream-http", complete,
                        result.get("wire_effort"), result.get("efforts_seen"), result.get("wire_efforts")))
    return rows


def sanitize_public(data):
    """Re-allowlist even our own schema: arbitrary extra fields never survive."""
    records = []
    for original in data["records"]:
        r = row(original.get("provider"), original.get("requested_model"),
                original.get("wire_models", []), original.get("reported_models", []),
                original.get("route_status"), original.get("requested_effort"),
                original.get("reported_effort"), original.get("evidence"), original.get("complete"),
                original.get("wire_effort"), original.get("reported_efforts"), original.get("wire_efforts"),
                original.get("backend"))
        if not r["complete"] and r["route_status"] == "MATCH":
            r["route_status"] = "UNKNOWN"
        if not r["reported_models"] and r["route_status"] == "MATCH":
            r["route_status"] = "UNKNOWN"
        if r["route_status"] == "MATCH":
            if (not r["wire_models"] or "[redacted]" in [r["requested_model"]] + r["wire_models"] + r["reported_models"]):
                r["route_status"] = "UNKNOWN"
            elif len(r["wire_models"]) > 1 or any(not same_model(r["wire_models"][0], m) for m in r["reported_models"]):
                r["route_status"] = "CHANGED"
            elif r["requested_model"].startswith(("claude-", "gpt-", "o1", "o3", "o4")) and not same_model(r["requested_model"], r["wire_models"][0]):
                r["route_status"] = "CHANGED"
        control = original.get("direct_control")
        if isinstance(control, dict):
            control_models = labels(control.get("reported_models", []))
            control_complete = control.get("complete") is True and bool(control_models) and "[redacted]" not in control_models
            r["direct_control"] = {
                "backend": "claude-direct",
                "route_status": control.get("route_status") if control_complete and control.get("route_status") in ROUTE_STATES else "UNKNOWN",
                "reported_models": control_models,
                "agrees": (control_complete and control.get("agrees") is True
                           and set(control_models) == set(r["reported_models"])),
                "complete": control_complete,
            }
        records.append(r)
    result = {"schema_version": 1, "tool": "Am I Nerfed?", "tool_version": __version__,
              "synthetic": data.get("synthetic") is True, "records": records,
              "limitation": LIMITATION}
    coverage = data.get("coverage")
    if isinstance(coverage, dict):
        counts = {}
        for key in ("planned_probes", "discovery_failures", "unsuccessful_runs"):
            value = coverage.get(key)
            counts[key] = value if type(value) is int and 0 <= value <= 10**9 else None
        counts["recorded_probes"] = len(records)
        counts["complete_probes"] = sum(complete_evidence(r) for r in records)
        counts["complete"] = (coverage.get("complete") is True
                              and counts["discovery_failures"] == 0
                              and counts["planned_probes"] == counts["complete_probes"] == len(records)
                              and bool(records))
        result["coverage"] = counts
    return result


def normalize(data):
    if not isinstance(data, dict):
        raise ValueError("Expected a JSON report object")
    if data.get("schema_version") == 1 and isinstance(data.get("records"), list):
        return sanitize_public(data)
    results = data.get("results")
    if not isinstance(results, list) or not all(isinstance(r, dict) for r in results):
        raise ValueError("Unrecognized report: expected a results array")
    if "claude_version" in data:
        records = _claude_rows(data)
    elif "codex_version" in data:
        records = _codex_rows(data)
    else:
        raise ValueError("Unrecognized provider report")
    return sanitize_public({"records": records})


def load_reports(paths):
    parts = [normalize(json.loads(Path(path).read_text(encoding="utf-8"))) for path in paths]
    result = {"records": [r for p in parts for r in p["records"]],
              "synthetic": any(p["synthetic"] for p in parts)}
    if parts and any("coverage" in part for part in parts):
        # Standalone provider reports cannot tell us whether tool discovery failed.
        # Preserve that uncertainty when mixed with an aggregate scan.
        for part in parts:
            part.setdefault("coverage", {"planned_probes": len(part["records"]), "discovery_failures": None,
                                         "unsuccessful_runs": None, "complete": False})
        result["coverage"] = {
            key: sum(p["coverage"][key] for p in parts) if all(type(p["coverage"][key]) is int for p in parts) else None
            for key in ("planned_probes", "discovery_failures", "unsuccessful_runs")}
        result["coverage"]["complete"] = all(p["coverage"]["complete"] for p in parts)
    return sanitize_public(result)


def markdown(data):
    data = sanitize_public(data)
    lines = ["# Am I Nerfed? · 降智测试", ""]
    if data["synthetic"]:
        lines += ["**SYNTHETIC DEMO — these are examples, not measured results.**", ""]
    if "coverage" in data:
        c = data["coverage"]
        lines += ["Coverage: %s — %s/%s probes have complete evidence; discovery failures: %s; unsuccessful runs: %s." %
                  ("COMPLETE" if c["complete"] else "INCOMPLETE", c["complete_probes"],
                   c["planned_probes"] if c["planned_probes"] is not None else "unknown",
                   c["discovery_failures"] if c["discovery_failures"] is not None else "unknown",
                   c["unsuccessful_runs"] if c["unsuccessful_runs"] is not None else "unknown"), ""]
    lines += ["| Backend | Selected | Wire request | Reported model | Route | Effort | Claude direct control |",
              "|---|---|---|---|---|---|---|"]
    for r in data["records"]:
        cols = [r["backend"], r["requested_model"], ", ".join(r["wire_models"]) or "not observed",
                ", ".join(r["reported_models"]) or "not reported", r["route_status"], r["effort_status"], control_label(r)]
        lines.append("| " + " | ".join(cols) + " |")
    lines += ["", "MATCH: observed model identifiers agree. CHANGED: an observable routing difference.",
              "UNKNOWN: insufficient or failed evidence. NOT_REPORTED: effort was not independently reported.",
              "", LIMITATION, "", "Generated locally. No credentials, account IDs, prompts, request IDs or raw errors are included.",
              "Review model labels before publishing. https://github.com/mushanyoung/am-i-nerfed", ""]
    return "\n".join(lines)


def control_label(record):
    control = record.get("direct_control")
    if not control:
        return "not run"
    comparable = control.get("complete") and record.get("complete") and bool(record.get("reported_models"))
    status = "UNKNOWN" if not comparable else ("AGREES" if control.get("agrees") else "DIFFERS")
    if control.get("complete") and control.get("route_status") == "CHANGED":
        status = "CHANGED"
    return status + ": " + (", ".join(control.get("reported_models", [])) or "not reported")


def svg(data):
    """Portable SVG share card; all variable strings are allowlisted + XML escaped."""
    data = sanitize_public(data)
    records = data["records"]
    if len(records) > 40:
        raise ValueError("SVG export supports up to 40 rows; use JSON or Markdown for larger runs")
    row_height = 114 if any(r.get("direct_control") for r in records) else 88
    height = 265 + len(records) * row_height
    e = html.escape
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="{height}" viewBox="0 0 1200 {height}" role="img" aria-labelledby="title desc">',
             '<title id="title">Am I Nerfed? model routing report</title>',
             '<desc id="desc">Provider-reported metadata. Not a model intelligence benchmark.</desc>',
             f'<rect width="1200" height="{height}" rx="24" fill="#111422"/>',
             '<path d="M48 56h24v28h28v-28h24" fill="none" stroke="#83f2c5" stroke-width="5"/>',
             '<g font-family="ui-monospace, SFMono-Regular, Consolas, monospace">',
             '<text x="148" y="80" fill="#f4f5fa" font-size="35" font-weight="700">Am I Nerfed?</text>',
             '<text x="48" y="126" fill="#a5aec5" font-size="20">Trace the model behind the answer.</text>']
    if data["synthetic"]:
        parts.append('<text x="1100" y="76" text-anchor="end" fill="#facc72" font-size="20">SYNTHETIC DEMO</text>')
    for i, r in enumerate(records):
        y = 166 + i * row_height
        color = {"MATCH": "#83f2c5", "CHANGED": "#facc72", "UNKNOWN": "#a5aec5"}[r["route_status"]]
        selected = r["requested_model"][:48]
        served = ", ".join(r["reported_models"])[:64] or "not reported"
        parts += [f'<rect x="36" y="{y-20}" width="1128" height="{row_height-10}" rx="8" fill="#1c2134"/>',
                  f'<text x="54" y="{y+5}" fill="#a5aec5" font-size="15">{e(r["backend"].upper())} / {e(selected)}</text>',
                  f'<text x="54" y="{y+35}" fill="#f4f5fa" font-size="20">{e(served)}</text>',
                  f'<text x="1126" y="{y+5}" text-anchor="end" fill="{color}" font-size="21">{r["route_status"]}</text>',
                  f'<text x="1126" y="{y+34}" text-anchor="end" fill="#a5aec5" font-size="14">effort: {r["effort_status"]}</text>']
        if r.get("direct_control"):
            parts.append(f'<text x="54" y="{y+66}" fill="#facc72" font-size="15">Direct CLI control — {e(control_label(r)[:100])}</text>')
    bottom = height - 52
    footer = "Model metadata is an observation, not proof of weights or intelligence."
    if "coverage" in data:
        c = data["coverage"]
        footer = "Coverage %s: %s/%s complete probes. Metadata cannot attest model weights." % (
            "COMPLETE" if c["complete"] else "INCOMPLETE", c["complete_probes"], c["planned_probes"])
    parts += [f'<text x="48" y="{bottom}" fill="#a5aec5" font-size="16">{e(footer)}</text>',
              f'<text x="48" y="{bottom+27}" fill="#83f2c5" font-size="15">github.com/mushanyoung/am-i-nerfed</text>', '</g></svg>']
    return "\n".join(parts) + "\n"


def render(data, format_name):
    if format_name == "json":
        return json.dumps(sanitize_public(data), indent=2, ensure_ascii=False) + "\n"
    return svg(data) if format_name == "svg" else markdown(data)


def demo():
    return sanitize_public({"synthetic": True, "records": [
        row("claude", "sonnet", ["claude-sonnet-example"], ["claude-sonnet-example"], "MATCH", "medium", evidence="synthetic", complete=True, backend="claude-proxy"),
        row("codex", "gpt-example-large", ["gpt-example-large"], ["gpt-example-small"], "CHANGED", "high", "high", "synthetic", True, backend="codex-cli"),
        row("codex", "gpt-example", ["gpt-example"], [], "UNKNOWN", "low", evidence="synthetic", backend="codex-http"),
    ]})
