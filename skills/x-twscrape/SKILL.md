---
name: x-twscrape
description: 使用登录态和 twscrape 抓取指定 X/Twitter 账号的推文、回复、媒体和账号范围关键词检索，保存 JSONL、原图及直接 PDF，支持增量轮询和限流状态检查。适用于复用 X 数据采集流程，不用于发布推文或绕过访问权限。
---

# X 推文抓取

复用本 Skill 的 `scripts/`，先运行 `x_watch.py --help` 了解入口。所有相对数据路径基于调用时的工作目录；脚本路径必须使用当前 Skill 的实际绝对路径。无需克隆原 Investor Watcher 项目。

## 执行入口

1. 明确目标 handle、时间范围、是否下载附件和输出目录。新增监控默认最近 20 分钟；历史补抓按用户明确给出的范围执行。
2. 在目标项目虚拟环境安装 `twscrape==0.18.1`、`certifi>=2026.5.20`。这是原项目依赖快照；升级先验证，不能据此承诺当前 X 接口可用。
3. 执行 `x_watch.py status --db <账号库> --output <输出目录>`。若无登录态，指导用户在本机运行 `init-account --username <本地标识>`，由隐藏提示输入 Cookie，或传入私密文件路径 `--cookies-file`。不要要求用户向聊天提交 Cookie，不读取并展示 Cookie 文件。初始化不联网，不覆盖已有同名账号。
4. 单次任务用 `run`；用户明确要求持续抓取时用 `watch`。使用相同账号库与输出目录的任务由文件锁防止并发。若已有生产任务使用该登录态，先复用现有任务，不另起并行抓取。
5. 检查进程结果、`last_run.json`、`run_metadata.json`、`errors.jsonl` 和附件 manifest（下载清单）。出现失败时报告具体阶段、已保存数量和尚未完成的范围。不要只以进程退出或报告里的 `ok` 宣称全部抓完。

命令模板，`<SKILL>` 和 `<PYTHON>` 需要替换为实际路径：

```bash
<PYTHON> <SKILL>/scripts/x_watch.py run --handles xdevelopers --lookback-minutes 20 --attachments
<PYTHON> <SKILL>/scripts/x_watch.py watch --handles xdevelopers --lookback-minutes 20 --interval-seconds 600 --replies-every 6 --attachments
<PYTHON> <SKILL>/scripts/x_watch.py run --handles xdevelopers --days 90 --modes timeline replies search --attachments
<PYTHON> <SKILL>/scripts/x_watch.py run --handles xdevelopers --lookback-minutes 20 --modes search --query-suffix 'AI lang:en'
```

原始数据在 `<输出目录>/tweets.jsonl`，附件在 `<输出目录>/attachments/`。任意账号可通过 `--handles` 显式指定，没有默认私人关注名单。

## 操作约束

- 默认只使用 `twscrape`。不自动切换可能收费的官方 X API，不买账号、代理或订阅。涉及新增费用需用户明确确认。
- 保持串行抓取。账号限流时等待其锁定时间，不清空锁、不重置额度、不自动替换登录态。
- Cookie、账号库、帖子正文、下载文件和生产配置不进入公开仓库。只在用户授权的登录态和内容权限内抓取。
- 采集器默认保留损坏 JSONL 原文件并报错；修复需要备份和明确授权，不自动删除记录。更换时间窗口时使用独立归档目录，避免误读旧检查点。
- 不擅自启动历史重分析、付费 AI 调用或 Telegram 推送。本 Skill 只采集并保存数据。

## 必须如实解释的限制

`watch` 每轮结束后等待 600 秒，不是严格每 10 分钟启动；20 分钟窗口在耗时过长或停机后可能漏帖。不能承诺即时或全量。

`Tweet.dict()` 是解析后的帖子对象，不是完整 GraphQL 响应。长推、引用帖和线程仅保存库实际返回的字段，不保证全文或整串上下文。PDF 只处理 `tweet.links` 中展开 URL 以 `.pdf` 标识的直接链接；不会进入文章页找报告或带登录下载报告。原图失败回退也有条件，具体见方案说明。

涉及覆盖报告、历史补抓、附件失败、迁移或上游接口故障时，读取 [references/implementation.md](references/implementation.md)。
