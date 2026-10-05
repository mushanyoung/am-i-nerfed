"""Discover installed clients and collect every selected model without fail-fast."""
import argparse
import json
import math
from pathlib import Path
import re

from . import discovery, reports, runtime

PROVIDERS = ("claude", "codex")


def selector(value):
    provider, sep, model = value.partition(":")
    if provider not in PROVIDERS or not sep or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.\[\]-]{0,119}", model):
        raise argparse.ArgumentTypeError("Use PROVIDER:MODEL, e.g. claude:opus or codex:gpt-example")
    return provider, model


def parser(inventory_only=False):
    p = argparse.ArgumentParser(prog="am-i-nerfed " + ("models" if inventory_only else "scan"),
        description="Discover installed Claude Code and Codex clients" if inventory_only else
        "Test every client-advertised model on every installed tool (uses subscription quota).")
    p.add_argument("--tool", action="append", choices=PROVIDERS, help="Limit tools; repeat to select both")
    p.add_argument("--include-hidden", action="store_true", help="Also include hidden catalog entries where available")
    p.add_argument("--discovery-timeout", type=float, default=30, help="Catalog deadline per tool in seconds")
    p.add_argument("-v", "--verbose", action="store_true", help="Show catalog details and provider diagnostics")
    if inventory_only:
        return p
    p.add_argument("-m", "--model", action="append", type=selector, help="Only these PROVIDER:MODEL pairs; repeatable")
    p.add_argument("--exclude-model", action="append", type=selector, default=[], help="Exclude a PROVIDER:MODEL pair; repeatable")
    p.add_argument("-n", "--repeat", type=int, default=1)
    p.add_argument("-e", "--effort", choices=sorted(reports.EFFORTS), help="Explicit effort for every model; unsupported values fail that model")
    p.add_argument("--timeout", type=float, default=120, help="Deadline per probe in seconds")
    p.add_argument("--out", type=Path, help="New output directory; defaults to ~/.am-i-nerfed/scan-TIMESTAMP on Linux, runs/scan-TIMESTAMP elsewhere")
    p.add_argument("--dry-run", action="store_true", help="Discover and print planned probes without inference")
    p.add_argument("--codex-transport", choices=("both", "cli", "http"), default="both",
                   help="Codex backends to probe (default: both)")
    controls = p.add_mutually_exclusive_group()
    controls.add_argument("--direct-control", dest="direct_control", action="store_true", default=True,
                          help="Also run Claude without the capture proxy (default)")
    controls.add_argument("--no-direct-control", dest="direct_control", action="store_false",
                          help="Skip the additional Claude direct CLI control")
    p.add_argument("--ignore-alias-overrides", action="store_true", help="Remove Claude alias environment overrides when probing")
    p.add_argument("--save-raw", action="store_true", help="Save private Claude response bodies locally")
    return p


def inventory(tool_names, timeout, include_hidden):
    installed = discovery.discover_tools()
    selected = list(dict.fromkeys(tool_names or [p for p in PROVIDERS if p in installed]))
    result = {"tools": {}, "failures": [], "scope": "Client-advertised candidates; availability is verified by each probe."}
    if not selected:
        result["failures"].append({"provider": "unknown", "reason": "no_installed_clients"})
    for provider in selected:
        if provider not in installed:
            result["failures"].append({"provider": provider, "reason": "client_not_installed"})
            continue
        try:
            entry = discovery.discover_models(provider, timeout=timeout, include_hidden=include_hidden)
            if not isinstance(entry, dict) or not isinstance(entry.get("models"), list):
                raise ValueError("invalid_catalog")
            if not all(isinstance(m, str) and selector(provider + ":" + m) for m in entry["models"]):
                raise ValueError("invalid_model")
            entry = dict(entry, executable=installed[provider])
            result["tools"][provider] = entry
            if not entry["models"]:
                result["failures"].append({"provider": provider, "reason": "empty_catalog"})
            elif "cache" in entry.get("source", "").lower():
                result["failures"].append({"provider": provider, "reason": "cached_catalog_only"})
        except Exception as exc:
            # Native errors can contain personal paths or server text; expose no error body.
            result["failures"].append({"provider": provider, "reason": "discovery_failed", "error_type": type(exc).__name__})
    return result


def choose_effort(provider, model, entry, override):
    if override:
        return override
    if provider == "claude":
        return None  # Preserve the client's per-model default.
    info = entry.get("catalog", {}).get(model, {})
    levels = [e for e in info.get("efforts", []) if isinstance(e, str) and e]
    default = info.get("default_effort")
    if isinstance(default, str) and default:
        return default
    if "low" in levels or not levels:
        return "low"
    return levels[0]


def plan_probes(pairs, found, args):
    plan = []
    for provider, model in pairs:
        backends = (["codex-cli", "codex-http"] if args.codex_transport == "both" else
                    ["codex-" + args.codex_transport]) if provider == "codex" else ["claude-proxy"]
        for backend in backends:
            plan.append({"provider": provider, "model": model, "backend": backend,
                         "effort": choose_effort(provider, model, found["tools"].get(provider, {}), args.effort),
                         "repeat": args.repeat, "direct_control": provider == "claude" and args.direct_control})
    return plan


def result_status(rows, status):
    if any(r["route_status"] == "CHANGED" or r["effort_status"] == "CHANGED"
           or (r.get("direct_control", {}).get("complete") is True
               and r["direct_control"].get("route_status") == "CHANGED")
           or (r.get("complete") is True and bool(r.get("reported_models"))
               and r.get("direct_control", {}).get("complete") is True
               and r["direct_control"].get("agrees") is not True) for r in rows):
        return "CHANGED"
    if status or not rows or any(r["route_status"] != "MATCH" or not reports.complete_evidence(r) for r in rows):
        return "UNKNOWN"
    return "MATCH"


def result_line(index, total, item, rows, status):
    reported = list(dict.fromkeys(m for r in rows for m in r["reported_models"]))
    routes = ",".join(dict.fromkeys(r["route_status"] for r in rows))
    efforts = ",".join(dict.fromkeys(r["effort_status"] for r in rows))
    line = "[%d/%d] %s %s → %s | %s route=%s effort=%s" % (
        index, total, item["backend"], item["model"], ",".join(reported) or "unknown", status, routes, efforts)
    if item["direct_control"]:
        controls = [r.get("direct_control", {}) for r in rows]
        control_status = ("CHANGED" if any(c.get("complete") is True and c.get("route_status") == "CHANGED" for c in controls) else
                          "UNKNOWN" if any(r.get("complete") is not True or not r.get("reported_models")
                                           or c.get("complete") is not True for r, c in zip(rows, controls)) else
                          "MATCH" if all(c.get("agrees") is True for c in controls) else "CHANGED")
        line += " direct=" + control_status
    return line + " | complete=%d/%d" % (sum(reports.complete_evidence(r) for r in rows), item["repeat"])


def main(argv, probe, private_write, inventory_only=False):
    p = parser(inventory_only)
    args = p.parse_args(argv)
    if not math.isfinite(args.discovery_timeout) or not 0 < args.discovery_timeout <= 3600:
        p.error("--discovery-timeout must be finite and between 0 and 3600 seconds")
    requested = None if inventory_only else args.model
    tools = args.tool
    if not inventory_only:
        if not 1 <= args.repeat <= 20 or not math.isfinite(args.timeout) or not 0 < args.timeout <= 3600:
            p.error("--repeat must be 1..20 and --timeout must be finite, greater than 0 and at most 3600")
        if tools and requested and any(provider not in tools for provider, _ in requested):
            p.error("Every --model provider must be included in --tool")
        if requested and not tools:
            tools = list(dict.fromkeys(provider for provider, _ in requested))
        if args.out and args.out.exists():
            raise ValueError("Output directory already exists; choose a new --out directory")
    found = inventory(tools, args.discovery_timeout, args.include_hidden)
    if inventory_only:
        print(json.dumps(found, ensure_ascii=False, indent=2))
        return 1 if found["failures"] else 0
    pairs = requested or [(provider, model) for provider, entry in found["tools"].items() for model in entry["models"]]
    pairs = [pair for pair in dict.fromkeys(pairs) if pair not in args.exclude_model]
    plan = plan_probes(pairs, found, args)
    found["plan"] = plan
    if args.dry_run:
        print(json.dumps(found, ensure_ascii=False, indent=2))
        return 1 if found["failures"] or not plan else 0
    console = runtime.Console(args.verbose)
    for failure in found["failures"]:
        console.warning("Discovery warning: %s / %s" % (failure["provider"], failure["reason"]))
    if not plan:
        console.error("No models selected; no inference requests were sent.")
        return 1
    out = args.out or runtime.default_output("scan")
    runtime.private_mkdir(out)
    private_write(out / "inventory.json", json.dumps(found, ensure_ascii=False, indent=2) + "\n")
    planned = len(plan) * args.repeat
    controls = sum(item["direct_control"] for item in plan) * args.repeat
    console.info("Am I Nerfed? — %d model(s); %d primary probe(s) + %d Claude direct control(s); output: %s" % (
        len(pairs), planned, controls, out))
    for provider, entry in found["tools"].items():
        console.detail("%s: %d candidates via %s" % (provider, len(entry["models"]), entry.get("source", "unknown")))
        for warning in entry.get("warnings", []):
            console.detail("  " + str(warning))
    records, run_statuses = [], []
    for index, item in enumerate(plan, 1):
        provider, model = item["provider"], item["model"]
        target = out / "probes" / (item["backend"] + "-%03d" % index)
        probe_args = ["--model", model, "--repeat", str(args.repeat), "--timeout", str(args.timeout), "--out", str(target)]
        probe_args.append("--verbose" if args.verbose else "--quiet")
        if item["effort"]:
            probe_args += ["--effort", item["effort"]]
        if item["backend"] == "codex-cli":
            probe_args.append("--via-codex")
        if provider == "claude":
            for name in ("direct_control", "ignore_alias_overrides", "save_raw"):
                if getattr(args, name):
                    probe_args.append("--" + name.replace("_", "-"))
        status, rows = 1, []
        try:
            entry = found["tools"].get(provider, {})
            levels = entry.get("catalog", {}).get(model, {}).get("efforts", [])
            if args.effort and levels and "cache" not in entry.get("source", "").lower() and args.effort not in levels:
                console.error("%s %s: requested effort is unsupported by the live catalog." % (item["backend"], model))
                raise ValueError("unsupported_effort")
            status = probe(provider, probe_args)
            if (target / "report.json").is_file():
                rows = reports.load_reports([target / "report.json"])["records"]
        except SystemExit as exc:
            status = exc.code if isinstance(exc.code, int) else 1
        except Exception as exc:
            console.error("%s %s: probe failed (%s); continuing." % (item["backend"], model, type(exc).__name__))
        if len(rows) != args.repeat:
            status = status or 1
        while len(rows) < args.repeat:
            rows.append(reports.row(provider, model, [], [], "UNKNOWN", item["effort"]))
        for row in rows:
            row["backend"] = item["backend"]
            if item["direct_control"]:
                row.setdefault("direct_control", {"reported_models": [], "agrees": False, "complete": False})
                row["direct_control"]["backend"] = "claude-direct"
        records.extend(rows)
        run_statuses.append(status)
        verdict = result_status(rows, status)
        console.result(result_line(index, len(plan), item, rows, verdict), verdict)
    complete = sum(reports.complete_evidence(r) for r in records)
    data = {"records": records, "coverage": {
        "planned_probes": planned, "recorded_probes": len(records), "complete_probes": complete,
        "discovery_failures": len(found["failures"]), "unsuccessful_runs": sum(bool(s) for s in run_statuses),
        "complete": not found["failures"] and len(records) == complete == planned,
    }}
    for format_name, filename in (("json", "report.json"), ("json", "share.json"), ("markdown", "share.md")):
        private_write(out / filename, reports.render(data, format_name))
    console.info("Completed evidence: %d/%d primary probes. Shareable summary: %s" % (complete, planned, out / "share.md"))
    failures = any(r["route_status"] != "MATCH" or r["effort_status"] == "CHANGED"
                   or (r.get("direct_control", {}).get("complete") is True
                       and r["direct_control"].get("route_status") == "CHANGED")
                   or ("direct_control" in r and r["direct_control"].get("agrees") is not True) for r in records)
    return 2 if not data["coverage"]["complete"] or any(run_statuses) or failures else 0
