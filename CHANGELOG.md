# Changelog

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
