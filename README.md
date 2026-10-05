<p align="center"><img src="assets/banner.svg" alt="降智测试 · Am I Nerfed? — 你点的模型，给你了吗？" width="960"></p>

<p align="center"><a href="README.en.md">English</a> · <a href="docs/methodology.md">检测原理</a> · <a href="CONTRIBUTING.md">参与贡献</a> · <a href="LICENSE">MIT</a></p>

# 降智测试 · Am I Nerfed?

[![CI](https://github.com/mushanyoung/am-i-nerfed/actions/workflows/ci.yml/badge.svg)](https://github.com/mushanyoung/am-i-nerfed/actions/workflows/ci.yml)

**你点的模型，给你了吗？**

一条命令扫描本机已安装的 Claude Code 和 Codex，检测客户端可发现的全部模型。对照请求与上游模型标识，记录别名、可见 fallback 和 reasoning effort，生成本地报告与可分享摘要。

```bash
am-i-nerfed
```

检测使用已有的**订阅登录**，真实请求会消耗相应服务的额度。默认覆盖所有发现的模型；要先看清单，用 `am-i-nerfed models` 或 `am-i-nerfed --dry-run`。

结果是本次请求的**可观察路由证据**。一致只表示可见模型标识一致，不能证明后台权重、排除隐藏账号标记或衡量模型智力。差异也需要区分别名、配置与 fallback。

## 安装

需要 Python 3.9+，运行时仅使用标准库。先安装官方 Claude Code / Codex CLI，并通过订阅账号登录。Codex 使用 `codex login` 选择 ChatGPT 登录；API key 是另一种认证路径。[OpenAI 官方认证说明](https://learn.chatgpt.com/docs/auth)

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install 'git+https://github.com/mushanyoung/am-i-nerfed.git'
am-i-nerfed
```

面向 macOS / Linux；Claude 探针不支持原生 Windows，可在 WSL 中使用。Codex 原生 Windows 路径尚未验证。安装来源是 GitHub 或本地 checkout，不要求 PyPI 上存在同名包。

## 全量扫描与范围选择

裸命令和 `am-i-nerfed scan` 等价。它们发现已安装工具，从客户端模型目录读取候选项，并逐一检测；一个工具或模型失败时，继续其余项，在结果中保留失败。

扫描默认通过真实客户端采集：Claude 经本机代理观察，Codex 使用 CLI 路径。需要 Codex HTTP 对照时显式设置 `--codex-transport http`。

```bash
# 查看工具与模型来源，不发送推理请求
am-i-nerfed models
am-i-nerfed scan --dry-run

# 只扫描一个工具；--tool 可重复
am-i-nerfed scan --tool claude

# 明确选择模型；MODEL 替换为 models 列出的 Codex ID
am-i-nerfed scan --model claude:opus --model codex:MODEL

# 排除模型；其余发现的模型仍会检测
am-i-nerfed scan --exclude-model claude:claude-haiku-4-5-20251001

# 调整重复次数、推理强度、单次超时和输出位置
am-i-nerfed scan --tool codex --repeat 2 --effort high --timeout 120 --out runs/check
```

`--model` 和 `--exclude-model` 使用 `提供方:模型` 格式，并可重复。设置 `--model` 后仅测试这些指定项，不再自动补全其他工具或模型；若同时设置 `--tool`，二者必须一致。排除规则按清单中的模型标识精确匹配：清单列出完整 ID 时，不能用 `haiku` 别名代替。显式 `--effort` 必须受目标模型支持。每次使用新的 `--out`，已有结果不会覆盖。

| 扫描选项 | 行为 |
| --- | --- |
| `--include-hidden` | 也尝试 Codex 目录中的隐藏条目；不保证账号可用，Claude 没有对应目录接口 |
| `--discovery-timeout 30` | 设置目录发现超时，默认 30 秒 |
| `--timeout 120` | 设置单次检测超时，默认 120 秒 |
| `--effort LEVEL` | 统一覆盖目标模型的 effort；默认 Codex 使用每模型目录默认值或支持的低强度，Claude 保留客户端默认值 |
| `--direct-control` | 为 Claude 增加绕过代理的 CLI 对照 |
| `--ignore-alias-overrides` | 在 Claude 子进程中移除家族别名覆盖 |
| `--save-raw` | 为 Claude 显式保存原始响应；文件应留在本地 |

`models` 同样支持 `--tool`、`--include-hidden` 和 `--discovery-timeout`。

“全部”指**本次客户端可发现、向用户提供的模型集合**，不代表服务端全部历史 ID，也不保证目录中的每个模型此刻都可调用。认证、客户端版本、缓存和账号权限会影响发现结果。显式选中的工具未安装、发现失败、目录回退或调用失败都会显示出来；不会因为某项失败就把它算作通过。`models` / `--dry-run` 不发推理请求，但发现过程可能需要启动官方客户端或读取目录。

## 单独复查一个提供方

保留提供方子命令，用于精确控制传输和对照。Claude 默认子命令仅检测 `haiku`；这与裸命令的全量扫描不同。

```bash
am-i-nerfed claude -m opus --direct-control --out runs/claude
am-i-nerfed claude -m opus --ignore-alias-overrides --out runs/claude-clean
am-i-nerfed codex -m MODEL --out runs/codex
am-i-nerfed codex -m MODEL --via-codex --out runs/codex-cli
```

Claude 路径启动真实客户端，通过仅监听本机的代理观察请求和响应。`--direct-control` 增加绕过代理的 CLI 元数据对照；它不捕获那次直连的原始 HTTP 响应。`--ignore-alias-overrides` 仅在检测子进程中移除别名覆盖。别名受 `ANTHROPIC_DEFAULT_*_MODEL` 影响，应比较**选择 → 实际请求 → 上游声明**。[Claude 模型配置](https://code.claude.com/docs/en/model-config)

Claude 客户端必须支持 `--safe-mode`；探针在空目录运行，关闭项目定制。这种隔离不会复现日常项目的完整上下文。

Codex 默认 HTTP 路径复用文件中的订阅凭据，观察响应头与流事件。**它使用实验性内部接口，兼容性可能随客户端更新变化。** `--via-codex` 运行真实 CLI，可使用客户端原生认证方式，包括系统密钥库。登录过期时通过官方客户端刷新；探针不改写凭据。两条路径的上下文和传输不同，应分别解读。

## 读懂结果

| 证据 | 回答的问题 |
| --- | --- |
| 用户选择与出站请求 | 本地别名或配置是否改变模型 |
| 响应头、开始 / 完成事件 | 上游声明了哪些模型标识 |
| fallback 事件与迭代记录 | 可见流中是否发生模型切换 |
| reasoning effort 元数据 | 请求与返回的推理强度是否可比较 |

共享摘要的 `route_status` 为 `MATCH` / `CHANGED` / `UNKNOWN`；`effort_status` 为 `MATCH` / `CHANGED` / `NOT_REPORTED` / `NOT_REQUESTED`。模型与 effort 分开判断。缺字段、流未完成或证据冲突不会被当成匹配；`CHANGED` 表示观察到差异，不是能力评分。

Claude 直连对照另列 `AGREES` / `DIFFERS` / `UNKNOWN`。Codex 即使模型一致，缺少所请求的 effort 返回值也可能给出非零退出码。一次短请求只覆盖该请求、该时间、该路径；全量扫描仍然不能代表长会话或整个订阅期。[检测原理](docs/methodology.md)

覆盖范围中的 `complete` 表示所计划检测具有完整证据，包括显式请求的直连对照；完整证据仍可能显示 `CHANGED`。覆盖完整与模型匹配是两个判断。

## 报告与离线演示

检测结果保存在输出目录；分享时使用生成的 `share.md` / `share.json`，或重新导出允许字段摘要。公开摘要排除账号、request ID、提示词、生成正文和原始错误，发布前仍应预览。

扫描目录包含 `inventory.json`（本地目录来源、客户端路径与警告）、`report.json`（汇总记录与覆盖范围）、`share.json` / `share.md`，以及 `probes/PROVIDER-NNN/` 下的逐模型诊断报告。清单和逐模型原始报告应保持私有；共享摘要仍保留扫描覆盖范围，避免把部分成功误读成全量成功。

```bash
am-i-nerfed report runs/check/report.json --format markdown --output scan-summary.md
am-i-nerfed report runs/claude/report.json --format markdown --output summary.md
am-i-nerfed report runs/codex/report.json --format json --output summary.json
am-i-nerfed report runs/claude/report.json runs/codex/report.json --format svg --output summary.svg

# 合成案例，不登录、不联网、不消耗额度
am-i-nerfed demo
am-i-nerfed demo --format svg --output demo.svg
```

[查看合成示例卡](examples/demo-card.svg) · [查看示例摘要](examples/demo-report.md)

完整诊断报告应留在本地。Claude 的 `--save-raw` 可显式保留原始响应，可能包含敏感内容；不要上传整个 `runs/`、凭据或 CLI 日志。项目不采集遥测，也不自动上传结果。[SECURITY.md](SECURITY.md)

## 常见问题

| 情况 | 处理 |
| --- | --- |
| 已安装工具发现失败 | 查看目录来源与错误；不要把缺失工具视为“已通过” |
| Claude 检测到 API key / gateway 配置 | 在仅使用官方订阅登录的终端运行 |
| CLI 缺少所需隔离参数 | 更新官方 CLI 后重试 |
| 模型已列出但调用失败 | 目录可用性与本次访问权限分别解读；保留失败结果 |
| 模型一致但 effort 缺失 | 保留 `NOT_REPORTED`，不把请求值当返回值 |

退出码 `0` 表示检测完成且所需可见检查一致，或清单 / 离线命令成功；`2` 表示差异或测量结论不完整；`1` 表示前置检查或运行错误。命令参数错误也可能返回 `2`。自动化应同时读取结果状态，不能只凭退出码断言“没有降智”。

## 开发与贡献

```bash
git clone https://github.com/mushanyoung/am-i-nerfed.git
cd am-i-nerfed
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
python -m unittest discover -s tests -v
```

自动化测试不需要外网或账号凭据；部分测试启动本机回环服务。真实订阅检测单独运行。欢迎提供可复现的目录发现、协议解析或报告问题，见 [CONTRIBUTING.md](CONTRIBUTING.md)。

独立社区项目，与 Anthropic、OpenAI 无隶属或背书关系。[MIT License](LICENSE)。
