"""Unified entry point for collectors and offline, allowlisted report export."""
import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import sys

from . import __version__
from . import reports
from .runtime import Console, default_output, private_mkdir


def private_write(path, text):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # O_EXCL also refuses symlinks and prevents accidental report overwrites.
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(text)


def _probe(provider, argv):
    from .providers import claude, codex
    module = claude if provider == "claude" else codex
    if "--help" in argv or "-h" in argv:
        if provider == "codex":
            print("Unified CLI output: --out DIRECTORY (Linux default: ~/.am-i-nerfed/codex-TIMESTAMP; otherwise runs/).")
            print("The provider's --json-out option below is managed internally.\n")
        return module.main(argv)
    wrapper = argparse.ArgumentParser(prog="am-i-nerfed " + provider, add_help=False)
    wrapper.add_argument("--out", type=Path)
    wrapper.add_argument("--quiet", action="store_true", help=argparse.SUPPRESS)
    wrapper.add_argument("-v", "--verbose", action="store_true")
    opts, remaining = wrapper.parse_known_args(argv)
    out = opts.out or default_output(provider)
    console = Console(verbose=opts.verbose)
    if out.exists():
        raise ValueError("Output directory already exists; choose a new --out directory")
    if any(arg == "--json-out" or arg.startswith("--json-out=") for arg in remaining):
        raise ValueError("Use --out DIRECTORY with am-i-nerfed; --json-out belongs to the provider API")
    if opts.verbose:
        remaining.append("--verbose")
    if provider == "codex":
        private_mkdir(out)
    provider_args = remaining + (["--out", str(out)] if provider == "claude" else ["--json-out", str(out / "report.json")])
    captured_error = io.StringIO()
    try:
        with contextlib.ExitStack() as stack:
            if opts.quiet:
                stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
                stack.enter_context(contextlib.redirect_stderr(captured_error))
            status = module.main(provider_args)
    finally:
        if opts.quiet and captured_error.getvalue().strip():
            # Preserve a concise preflight/error reason without duplicating help,
            # per-model diagnostics, or the scan's own result lines.
            console.error(captured_error.getvalue().strip().splitlines()[-1])
    report_path = out / "report.json"
    if report_path.is_file():
        public = reports.load_reports([report_path])
        for format_name, filename in (("json", "share.json"), ("markdown", "share.md")):
            private_write(out / filename, reports.render(public, format_name))
        if not opts.quiet:
            console.info("Report: " + str(out / "share.md"))
    return status or 0


def _offline(command, argv):
    parser = argparse.ArgumentParser(prog="am-i-nerfed " + command)
    if command == "report":
        parser.add_argument("reports", type=Path, nargs="+", help="Local provider report.json or share.json files")
    parser.add_argument("--format", choices=("markdown", "json", "svg"), default="markdown")
    parser.add_argument("--output", "-o", type=Path, help="Write a new file instead of stdout")
    args = parser.parse_args(argv)
    data = reports.demo() if command == "demo" else reports.load_reports(args.reports)
    output = reports.render(data, args.format)
    if args.output:
        private_write(args.output, output)
        print("Written:", args.output)
    else:
        print(output, end="")
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(
        prog="am-i-nerfed", description="降智测试 · Trace the model behind the answer.",
        epilog="Reports contain provider claims, not proof of model weights. Run demo without credentials or network.")
    parser.add_argument("--version", action="version", version="am-i-nerfed " + __version__)
    parser.add_argument("command", nargs="?", choices=("scan", "models", "claude", "codex", "report", "demo"),
                        help="Default: scan every discovered model on every installed tool")
    if argv and argv[0] in ("--help", "-h", "--version"):
        parser.parse_args(argv)
        return 0
    try:
        if not argv or argv[0].startswith("-") or argv[0] in ("scan", "models"):
            from . import scan
            command = argv.pop(0) if argv and argv[0] in ("scan", "models") else "scan"
            return scan.main(argv, _probe, private_write, inventory_only=command == "models")
        args = parser.parse_args(argv[:1])
        if args.command in ("claude", "codex"):
            return _probe(args.command, argv[1:])
        return _offline(args.command, argv[1:])
    except KeyboardInterrupt:
        Console().warning("Interrupted; completed local evidence is retained.")
        return 130
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        # OSError messages include local paths but do not include response bodies.
        # Parsing errors expose no private input text.
        if isinstance(exc, json.JSONDecodeError):
            message = "Invalid JSON report; private report content was not printed"
        elif isinstance(exc, (KeyError, TypeError, AttributeError)):
            message = "Malformed report or unsupported provider schema"
        else:
            message = str(exc)
        Console().error("am-i-nerfed: " + message)
        return 1
