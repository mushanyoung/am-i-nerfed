# Changelog

## 0.4.0

- Cover both existing subscription paths by default in scans: Claude proxy capture + direct-client control, and Codex CLI + HTTP. Label backends in reports and preserve each path's evidence independently.
- Default `--codex-transport` to `both`, with `cli` and `http` available for narrower checks. Add `--no-direct-control` for disabling the default Claude scan control; retain `--direct-control` compatibility.
- Store probe runs under `~/.am-i-nerfed/` on Linux across entry points. Other platforms retain the current directory's `runs/`; explicit `--out` always takes precedence.
- Keep terminal output concise by default, add detailed output through `-v` / `--verbose`, and use automatic terminal colors with `NO_COLOR` support.
- Consolidate maintained directories into `docs/`, `src/`, and `tests/`. Move the banner, logo, and demo SVG into `docs/`, and move `build_standalone.py` to the repository root.
- Remove redundant provider wrapper scripts and checked-in demo JSON / Markdown; provider subcommands and `demo --format` provide those entry points and examples.

Backend coverage is limited to the implemented subscription paths. It does not add Bedrock, Vertex, API key authentication, or model-weight attestation.

## 0.3.0

- Add a generated `am-i-nerfed.py` entry point that runs with Python 3.9+ without pip, git, a virtual environment, or package installation.
- Support direct piping into `python3 -`, downloading the file for reuse, and running it from a checkout. All paths accept the same CLI arguments and default to the full scan.
- Embed release code in the standalone file. Load it from a private temporary ZIP and clean it up on exit, without downloading additional project code at runtime.
- Preserve reports in the caller's current working directory under `runs/`, or the selected `--out` path.
- Add a standalone builder and `--check` for keeping the generated entry point synchronized with source.
- Make the standalone quick start the primary documentation path while retaining optional package installation for a persistent `am-i-nerfed` command.

Live probes still require the relevant official client and subscription sign-in, and consume the subscription allowance. The synthetic demo requires neither credentials nor inference requests.

## 0.2.0

Public release of **Am I Nerfed? · 降智测试** under the `am-i-nerfed` package and command.

- Make the bare command scan all installed supported tools and their discoverable user-facing models.
- Add `scan`, `models`, and dry-run inventory, with explicit tool / model selection and exclusions.
- Discover candidates from client catalogs and expose discovery or measurement failures while continuing other targets.
- Use real CLI transports by default for scans; retain an explicit Codex HTTP transport option.
- Retain Claude Code and Codex provider subcommands for transport-specific investigation.
- Keep requested model, response metadata, visible fallback, and reasoning effort as distinct evidence.
- Provide local diagnostic reports and summaries exported from allowed fields as Markdown, JSON, or SVG.
- Include an offline synthetic demo and synthetic regression tests without external network or account credentials.
- Document evidence limits, privacy boundaries, installation, and community contributions in Chinese and English.

“All models” means the models discovered from the installed clients in that run, not every historical server ID. Codex's direct HTTP transport uses an experimental internal interface. Matching metadata does not attest to backend model weights or rule out hidden account flags.
