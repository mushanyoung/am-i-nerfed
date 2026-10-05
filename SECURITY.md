# Security and privacy

Am I Nerfed? runs locally and reuses the relevant CLI's existing subscription authentication. It has no telemetry and does not automatically upload reports. Live requests are sent to the relevant provider; local-only reporting does not mean the probe works offline.

The bare command scans every discovered model from installed supported clients and uses subscription allowance. `models` and `--dry-run` perform discovery without inference; discovery can still start official clients. The `demo` command is entirely synthetic and offline.

## Handling reports

Full diagnostic reports and `inventory.json` may contain request identifiers, local client paths, environment details, response metadata, or errors. Per-model artifacts under `probes/` are diagnostic material. Claude's opt-in `--save-raw` can retain response content. CLI tools may also create their own logs outside the project's output directory. Treat these as private.

To share results, use `am-i-nerfed report ...` or a generated `share.md` / `share.json`, then preview the output. The exporter constructs a new document from allowed fields; this does not make the original report safe to upload. Never share authentication caches, token values, account IDs, or whole run directories.

The Claude capture proxy listens on loopback for the duration of a probe and forwards to a fixed official upstream. It is a local diagnostic component, not a shared or internet-facing proxy. Codex's direct transport uses an experimental internal endpoint; protocol compatibility may change.

## Reporting a vulnerability

For a credential disclosure, unsafe report export, unintended request forwarding, or similar security issue, use GitHub's [private vulnerability reporting](https://github.com/mushanyoung/am-i-nerfed/security/advisories/new). Include a minimal reproduction with synthetic values. Do not put real tokens in the report.

If private reporting is unavailable, open a public issue saying only that you need a private contact channel, without exploit details or sensitive data. Ordinary compatibility problems can use the normal issue templates.

If a credential was already exposed, revoke or rotate it through the relevant provider and remove the public artifact. Deleting a commit or issue does not invalidate a copied credential.

The latest released version receives security fixes on a best-effort basis. This community project does not promise a response SLA. Findings and fixes will be described without publishing affected users' data.

## 中文说明

完整报告、原始响应和 CLI 日志保留在本地。分享时使用导出的允许字段摘要并预览；不要上传登录缓存、token、账号 ID 或整个运行目录。发现凭据泄露、摘要越界导出等问题，请通过上面的 GitHub 私密漏洞报告入口提交合成复现。入口不可用时，公开 issue 只请求私下联系渠道，不披露敏感内容。
