# X Twscrape Skill

从 Investor Watcher 提取的 X 推文抓取工具和 Codex Skill。使用自己的 X 登录态，通过 `twscrape` 获取指定账号的帖子，保存正文、链接、媒体元数据，并下载图片和直接 PDF 链接。

本仓库以 MIT 协议开源。无需官方 X API Key，默认没有官方 API 计费调用；机器、网络和下游 AI 分析仍可能产生费用。只使用自己有权访问的内容。

## 两个独立的 Skill

| Skill | 用途 | 需要什么 |
| --- | --- | --- |
| [`x-twscrape`](skills/x-twscrape/SKILL.md) | 在自己的环境抓取 X 帖子、图片和直接 PDF | 自己有权使用的 X 登录态、Python 抓取环境 |
| [`investor-watcher-mcp`](skills/investor-watcher-mcp/SKILL.md) | 查询 Investor Watcher 已保存的帖子、投资观点、喊单和附件 | 支持 Streamable HTTP 的 MCP 客户端、服务所有者授权的 MCP 凭证 |

两个 Skill 可分别安装。只查询云端数据不需要安装本仓库的 Python 抓取依赖，也不需要 X Cookie。**Skill 开源不代表云端数据匿名开放或免费授权**；本仓库和下载包均不提供访问凭证，不包含生产数据或 MCP 服务端部署代码。

## 能力与边界

- 指定任意 X handle，支持 timeline（账号帖子列表）、replies（帖子及回复）、media（媒体列表）、search（账号范围检索）。
- 支持最近 20 分钟、历史 90 天或明确日期范围；按帖子 ID 去重。
- 串行抓取和随机等待，保留 `twscrape` 的限流冷却状态。
- 优先下载原图；只识别展开后 URL 中带 `.pdf` 的直接 PDF 链接。默认不下载视频。
- 提供本地状态查询、错误记录、覆盖情况报告，以及默认每轮结束后等待 600 秒的轮询入口。

**不是即时推送，也不承诺全量抓取。** X 返回范围、搜索收录、账号权限和限流都会影响结果。这里的“原始推文”是 `twscrape` 解析后的 `Tweet.dict()`，并非原封不动的 HTTP 响应。详细限制见[方案说明](skills/x-twscrape/references/implementation.md)。

## 快速使用

适用 macOS / Linux，建议 Python 3.12 或 3.13；完整的新依赖清单最低要求 Python 3.11.5（由 SoupSieve 3.0.4 决定）。建议先在独立目录安装，不要复用生产账号库同时运行两套任务。

```bash
git clone https://github.com/WilsonWang01/x-twscrape-skill.git
cd x-twscrape-skill
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

依赖锁定到修复后验证的 `twscrape==0.20.1`。2026-10-28 实测发现旧版 0.18.1 的网页签名解析已经失效，并可能把失败误报为“0 条新增”。本版停用旧签名补丁，使用上游的新格式支持，同时将请求中止、错误响应和未完成扫描明确报错。若遇到后续接口变化，仍应先在独立账号库和输出目录验证升级。上游说明：[vladkens/twscrape](https://github.com/vladkens/twscrape)。

本次实际测试读取了 Jukan 的账号资料、5 条帖子正文、3 条搜索结果，并成功下载 1 张原图。这是有限样本，不代表所有账号、历史窗口或 PDF 外站均已验证。库的使用统计上报默认关闭（`TWS_TELEMETRY=0`）。

抓取运行所需的 15 项依赖已统一核对并固定在 `skills/x-twscrape/requirements.txt`。除核心库外，本次更新了 BeautifulSoup、SoupSieve、AnyIO、idna、PyOTP、typing-extensions；HTTPX 等已是当前版本的包保留原版本。没有接入新的抓取引擎或修改下游 AI 分析器。

完整依赖升级后，云端 Python 3.12 环境的单账号 20 分钟窗口验证抓到 2 条新帖，错误数为 0。本机 Python 3.13 环境的 10 项离线测试通过。该结果仍不代表全部账号或历史数据完整。

### 1. 在本机导入登录态

在浏览器登录 X 后，从开发者工具中取得包含 `auth_token` 和 `ct0` 的 Cookie header（Cookie 请求头）。运行下面命令后在隐藏输入提示里粘贴，内容只写入本机账号库：

```bash
.venv/bin/python skills/x-twscrape/scripts/x_watch.py init-account \
  --username my-x-session
```

也可以指定自己保存的私密文件：

```bash
chmod 600 /private/path/x-cookies.txt
.venv/bin/python skills/x-twscrape/scripts/x_watch.py init-account \
  --username my-x-session --cookies-file /private/path/x-cookies.txt
```

格式示意，不能直接登录：`auth_token=<YOUR_LOCAL_AUTH_TOKEN>; ct0=<YOUR_LOCAL_CT0>`。
不要把真实值放到聊天、命令行参数、GitHub Issue 或仓库。账号库 `data/twscrape/accounts.db` 同样属于凭据。导入不访问 X，不清空限流锁，也不会覆盖同名账号。登录态更新需在本机明确处理，不能靠重置限流恢复登录。

### 2. 抓取一轮新增帖子和附件

将示例账号改成自己的目标账号：

```bash
.venv/bin/python skills/x-twscrape/scripts/x_watch.py run \
  --handles xdevelopers --lookback-minutes 20 --attachments
```

输出默认保存在当前目录下的 `data/x_archive/`。重复使用同一个输出目录才能跨轮去重。

### 3. 持续轮询

```bash
.venv/bin/python skills/x-twscrape/scripts/x_watch.py watch \
  --handles xdevelopers --lookback-minutes 20 \
  --interval-seconds 600 --replies-every 6 --attachments
```

每轮串行处理所有目标账号，结束后等待 600 秒，第 6、12 等轮加入回复扫描。进程退出即停止，按 `Ctrl+C` 结束。这里没有安装常驻服务或 GitHub Actions；长期运行可接入自己的进程管理器。重启后轮数从 1 开始。

固定 20 分钟窗口无法弥补长时间停机或慢任务造成的所有缺口；发现缺口后，先确认需要补抓的范围，再明确执行历史任务。延迟包括轮询间隔、抓取耗时和限流等待。

### 4. 历史范围与账号检索

抓取历史 90 天（按 UTC 日界线计算，默认结束日期为次日，不是精确滚动 90×24 小时）：

```bash
.venv/bin/python skills/x-twscrape/scripts/x_watch.py run \
  --handles xdevelopers --days 90 --modes timeline replies search --attachments
```

检索最近 20 分钟内该账号包含关键词的帖子：

```bash
.venv/bin/python skills/x-twscrape/scripts/x_watch.py run \
  --handles xdevelopers --lookback-minutes 20 \
  --modes search --query-suffix 'AI lang:en'
```

`--query-suffix` 只对 search 模式生效。明确日期可用底层脚本的 `--since YYYY-MM-DD --until YYYY-MM-DD`，后者为不含当天的结束边界。底层脚本没有进程锁，直接调用时必须自行确保无其他抓取任务。

### 5. 查询状态

```bash
.venv/bin/python skills/x-twscrape/scripts/x_watch.py status
```

只查询本地数据，输出帖子和错误行数、最近一次任务结果、账号 active/locks/stats（启用状态、冷却截止时间、请求统计），不会输出 Cookie、密码或完整账号记录。此命令不能单独证明后台进程仍存活或 X 登录态仍有效。

## 安装抓取 Skill

仓库里的 `skills/x-twscrape/` 是完整 Skill，包含所需脚本和方案说明。复制到本机 Codex 的 skills 目录即可；以下命令会在同名目录存在时停止，不会覆盖：

```bash
mkdir -p ~/.codex/skills
.venv/bin/python -c 'import shutil; from pathlib import Path; shutil.copytree("skills/x-twscrape", Path.home()/".codex/skills/x-twscrape")'
```

在新会话中使用，例如：

> 使用 $x-twscrape，抓取指定账号最近 20 分钟的推文和图片。先检查本地账号状态，保持单进程，完成后报告新增、失败与遗漏风险。

使用安装后的脚本仍需在项目虚拟环境执行 `python -m pip install -r ~/.codex/skills/x-twscrape/requirements.txt` 安装固定依赖。若目标环境的 Skill 目录不同，以该环境配置为准。

## 接入云端 MCP Skill

完整 Skill 在 `skills/investor-watcher-mcp/`，包含 Agent 工作指引、工具依赖声明和[接入说明](skills/investor-watcher-mcp/references/connection.md)。也可从 [Releases](https://github.com/WilsonWang01/x-twscrape-skill/releases/tag/mcp-skill-v1.0.0) 下载独立 ZIP 和 SHA-256 校验文件；ZIP 仅包含该 Skill 与 MIT 许可证。

克隆仓库后可将 Skill 安装到 Codex 用户技能目录；同名目标已存在时以下命令会停止，不覆盖：

```bash
python3 -c 'import shutil; from pathlib import Path; shutil.copytree("skills/investor-watcher-mcp", Path.home()/".agents/skills/investor-watcher-mcp")'
```

安装 Skill 不会自动连接 MCP。按接入说明配置客户端，并通过安全方式注入独立访问凭证。Codex 配置示例不包含真实值：

```toml
[mcp_servers.investor-watcher]
url = "https://investorwatcher.watch/mcp"
bearer_token_env_var = "INVESTOR_WATCHER_MCP_TOKEN"
```

实际运行客户端的进程需要获得 `INVESTOR_WATCHER_MCP_TOKEN`，值不含 `Bearer ` 前缀。不要把真实值提交到 Git、Issue 或下载包；本服务没有 OAuth 登录流程。其他 Agent 的配置字段和技能目录可能不同，以其官方文档为准。

连接并发现工具后，例如：

> 使用 $investor-watcher-mcp 汇总北京时间今天关注账号的看多喊单，附原文链接和数据最新时间。

此 Skill 只能查询已保存数据，不会触发抓取、补跑历史、修改订阅或发送 Telegram。一般看多观点与喊多信号分别使用 `direction` 与 `call_signal`；日期、分页、附件凭证保护和错误处理细节见 Skill，不能将数据库查询结果当作已核实的交易建议。

## 验证

```bash
python3 -m unittest discover -s tests -v
```

离线测试不访问 X、不读取真实 Cookie；验证 UTC 窗口、去重、请求中止不误报空结果、旧版依赖拒绝、部分失败退出状态、进程锁、附件候选、文件落盘和重跑跳过。通过这些测试本身不等于已验证实时抓取，上面的实际测试是另行完成的有限样本验证。

## 文件说明

| 路径 | 用途 |
| --- | --- |
| `skills/x-twscrape/SKILL.md` | Agent 调用指引 |
| `skills/x-twscrape/references/implementation.md` | 当前项目方案、数据格式、已知限制与迁移说明 |
| `skills/x-twscrape/scripts/collect_x_tweets_90d.py` | 从原项目提取的采集器，支持任意显式账号列表 |
| `skills/x-twscrape/scripts/download_x_attachments.py` | 从原项目提取的图片和直接 PDF 下载器 |
| `skills/x-twscrape/scripts/x_watch.py` | 凭据导入、进程锁、轮询和状态查询 |
| `skills/investor-watcher-mcp/SKILL.md` | 只读 MCP 的 Agent 查询流程、工具参数与安全边界 |
| `skills/investor-watcher-mcp/references/connection.md` | Codex 和其他 MCP 客户端接入说明 |
| `skills/investor-watcher-mcp/agents/openai.yaml` | Skill 展示信息及远程 MCP 工具依赖声明 |

本包不包含 Investor Watcher 的网页、生产数据库、服务器配置、Codex 分析服务或 Telegram 凭据。抓取结果可由其他项目自行读取 JSONL（每行一个 JSON 对象）接入。
