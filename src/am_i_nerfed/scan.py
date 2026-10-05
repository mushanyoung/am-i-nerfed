"""Discover installed clients and collect every selected model without fail-fast."""
import argparse
import datetime as dt
import json
import math
from pathlib import Path
import re
import sys

from . import discovery, reports

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
    if inventory_only:
        return p
    p.add_argument("-m", "--model", action="append", type=selector, help="Only these PROVIDER:MODEL pairs; repeatable")
    p.add_argument("--exclude-model", action="append", type=selector, default=[], help="Exclude a PROVIDER:MODEL pair; repeatable")
    p.add_argument("-n", "--repeat", type=int, default=1)
    p.add_argument("-e", "--effort", choices=sorted(reports.EFFORTS), help="Explicit effort for every model; unsupported values fail that model")
    p.add_argument("--timeout", type=float, default=120, help="Deadline per probe in seconds")
    p.add_argument("--out", type=Path, help="New output directory; defaults to runs/scan-TIMESTAMP")
    p.add_argument("--dry-run", action="store_true", help="Discover and print planned probes without inference")
    p.add_argument("--codex-transport", choices=("cli", "http"), default="cli")
    p.add_argument("--direct-control", action="store_true", help="Also run Claude without the capture proxy")
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
    plan = [{"provider": provider, "model": model,
             "effort": choose_effort(provider, model, found["tools"].get(provider, {}), args.effort),
             "repeat": args.repeat} for provider, model in pairs]
    found["plan"] = plan
    if args.dry_run:
        print(json.dumps(found, ensure_ascii=False, indent=2))
        return 1 if found["failures"] or not plan else 0
    if not plan:
        print(json.dumps(found, ensure_ascii=False, indent=2))
        print("No models selected; no inference requests were sent.", file=sys.stderr)
        return 1
    out = args.out or Path("runs") / ("scan-" + dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f"))
    out.mkdir(mode=0o700, parents=True)
    private_write(out / "inventory.json", json.dumps(found, ensure_ascii=False, indent=2) + "\n")
    print("Am I Nerfed? — %d model(s) × %d probe(s); output: %s" % (len(plan), args.repeat, out), flush=True)
    for provider, entry in found["tools"].items():
        print("%s: %d candidates via %s" % (provider, len(entry["models"]), entry.get("source", "unknown")), flush=True)
        for warning in entry.get("warnings", []):
            print("  " + str(warning), flush=True)
    for failure in found["failures"]:
        print("Discovery warning: %s / %s" % (failure["provider"], failure["reason"]), file=sys.stderr)
    records, run_statuses = [], []
    for index, item in enumerate(plan, 1):
        provider, model = item["provider"], item["model"]
        target = out / "probes" / (provider + "-%03d" % index)
        probe_args = ["--model", model, "--repeat", str(args.repeat), "--timeout", str(args.timeout), "--out", str(target)]
        if item["effort"]:
            probe_args += ["--effort", item["effort"]]
        if provider == "codex" and args.codex_transport == "cli":
            probe_args.append("--via-codex")
        if provider == "claude":
            for name in ("direct_control", "ignore_alias_overrides", "save_raw"):
                if getattr(args, name):
                    probe_args.append("--" + name.replace("_", "-"))
        print("[%d/%d] %s:%s" % (index, len(plan), provider, model), flush=True)
        status, rows = 1, []
        try:
            entry = found["tools"].get(provider, {})
            levels = entry.get("catalog", {}).get(model, {}).get("efforts", [])
            if args.effort and levels and "cache" not in entry.get("source", "").lower() and args.effort not in levels:
                print("Requested effort is unsupported by the live catalog; this model remains UNKNOWN.", file=sys.stderr)
                raise ValueError("unsupported_effort")
            status = probe(provider, probe_args)
            if (target / "report.json").is_file():
                rows = reports.load_reports([target / "report.json"])["records"]
        except SystemExit as exc:
            status = exc.code if isinstance(exc.code, int) else 1
        except Exception as exc:
            print("Probe failed (%s); continuing with remaining models." % type(exc).__name__, file=sys.stderr)
        if len(rows) != args.repeat:
            status = status or 1
        while len(rows) < args.repeat:
            rows.append(reports.row(provider, model, [], [], "UNKNOWN", item["effort"]))
        if provider == "claude" and args.direct_control:
            for row in rows:
                row.setdefault("direct_control", {"reported_models": [], "agrees": False, "complete": False})
        records.extend(rows)
        run_statuses.append(status)
    planned = len(plan) * args.repeat
    complete = sum(reports.complete_evidence(r) for r in records)
    data = {"records": records, "coverage": {
        "planned_probes": planned, "recorded_probes": len(records), "complete_probes": complete,
        "discovery_failures": len(found["failures"]), "unsuccessful_runs": sum(bool(s) for s in run_statuses),
        "complete": not found["failures"] and len(records) == complete == planned,
    }}
    for format_name, filename in (("json", "report.json"), ("json", "share.json"), ("markdown", "share.md")):
        private_write(out / filename, reports.render(data, format_name))
    print("Completed evidence: %d/%d probes. Shareable summary: %s" % (complete, planned, out / "share.md"))
    failures = any(r["route_status"] != "MATCH" or r["effort_status"] == "CHANGED"
                   or ("direct_control" in r and r["direct_control"].get("agrees") is not True) for r in records)
    return 2 if not data["coverage"]["complete"] or any(run_statuses) or failures else 0
