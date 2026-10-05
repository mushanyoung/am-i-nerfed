# Contributing to Am I Nerfed?

欢迎提交中文或英文 issue / PR。最有帮助的贡献是模型目录发现、协议变更适配、可复现的解析问题，以及文档中不准确的结论。

## 本地检查

```bash
git clone https://github.com/mushanyoung/am-i-nerfed.git
cd am-i-nerfed
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
python -m unittest discover -s tests -v
am-i-nerfed demo
```

Python 3.9+；运行时保持标准库依赖。测试使用合成响应，不访问外网或读取账号凭据；允许启动本机回环 HTTP 服务验证传输行为。真实订阅测试会消耗额度，不放入公共 CI，也不要求贡献者提供登录凭据。

## 提交问题

使用 [issue 模板](https://github.com/mushanyoung/am-i-nerfed/issues/new/choose)，提供版本、操作系统、简化命令、预期 / 实际行为和分享摘要。先检查完整模型 ID、别名覆盖和传输路径是否一致。不要提交 `runs/`、token、账号 ID、真实提示词、CLI 完整日志或原始响应。

如果需要新 fixture，请手工构造最小响应结构，使用明显虚构的模型名和标识。删掉敏感字段不代表剩余正文适合公开；优先从空文件重建合成样本。

## 修改约定

- 保留用户选择、实际请求和响应声明的区别；不要把别名解析误判为服务端切换。
- 默认扫描覆盖所有发现的模型；目录或调用失败需要显式保留，不能静默缩小候选集。
- 新增模型发现来源时记录来源和可用性限制；使用合成目录测试重复项、隐藏项、回退和失败。
- 缺少证据应保留未知。不能因流开始匹配，就忽略中途 fallback、失败或缺少完成事件。
- 模型与 effort 分开判定；模型自称、延迟和回答质量不充当路由证据。
- 公开导出继续使用允许字段清单；新增字段要考虑敏感内容和无意传播原始错误。
- 涉及协议解析或判定规则的改动，补足能区分正确 / 错误行为的合成回归测试。
- CLI 行为变化同步更新中英文 README 和 changelog。

PR 说明写清具体问题、改后行为和验证方式。无需先开 issue 才能修复小问题。MIT 许可适用于提交到本项目的贡献；只提交你有权公开的内容。

## English notes

Use synthetic, minimal fixtures and tests without external network or account credentials; loopback HTTP tests are allowed. Keep selection, request, and reported model evidence distinct; retain unknown when evidence is incomplete. Assess effort independently. Never commit credentials, private run reports, or real prompt / response content. Document behavior changes in both READMEs and the changelog. Security issues follow [SECURITY.md](SECURITY.md).

Default scans cover all discovered candidates. Discovery and inference failures must remain visible. Model catalog fixtures should exercise duplicate entries, hidden models, fallback sources, and unavailable tools without relying on real account data.
