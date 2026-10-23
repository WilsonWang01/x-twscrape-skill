# X Twscrape Skill

从 Investor Watcher 提取的 X 推文抓取工具和 Codex Skill。使用自己的 X 登录态，通过 `twscrape` 获取指定账号的帖子，保存正文、链接、媒体元数据，并下载图片和直接 PDF 链接。

本仓库以 MIT 协议开源。无需官方 X API Key，默认没有官方 API 计费调用；机器、网络和下游 AI 分析仍可能产生费用。只使用自己有权访问的内容。

## 能力与边界

- 指定任意 X handle，支持 timeline（账号帖子列表）、replies（帖子及回复）、media（媒体列表）、search（账号范围检索）。
- 支持最近 20 分钟、历史 90 天或明确日期范围；按帖子 ID 去重。
- 串行抓取和随机等待，保留 `twscrape` 的限流冷却状态。
- 优先下载原图；只识别展开后 URL 中带 `.pdf` 的直接 PDF 链接。默认不下载视频。
- 提供本地状态查询、错误记录、覆盖情况报告，以及默认每轮结束后等待 600 秒的轮询入口。

**不是即时推送，也不承诺全量抓取。** X 返回范围、搜索收录、账号权限和限流都会影响结果。这里的“原始推文”是 `twscrape` 解析后的 `Tweet.dict()`，并非原封不动的 HTTP 响应。详细限制见[方案说明](skills/x-twscrape/references/implementation.md)。

## 快速使用

适用 macOS / Linux，Python 3.10+。建议先在独立目录安装，不要复用生产账号库同时运行两套任务。

```bash
git clone https://github.com/WilsonWang01/x-twscrape-skill.git
cd x-twscrape-skill
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

依赖锁定到原项目使用的 `twscrape==0.18.1`，不是“始终兼容 X 当前接口”的保证。若遇到前端或接口变化，应先在独立账号库和输出目录验证上游升级。上游说明：[vladkens/twscrape](https://github.com/vladkens/twscrape)。

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

## 安装 Skill

仓库里的 `skills/x-twscrape/` 是完整 Skill，包含所需脚本和方案说明。复制到本机 Codex 的 skills 目录即可；以下命令会在同名目录存在时停止，不会覆盖：

```bash
mkdir -p ~/.codex/skills
.venv/bin/python -c 'import shutil; from pathlib import Path; shutil.copytree("skills/x-twscrape", Path.home()/".codex/skills/x-twscrape")'
```

在新会话中使用，例如：

> 使用 $x-twscrape，抓取指定账号最近 20 分钟的推文和图片。先检查本地账号状态，保持单进程，完成后报告新增、失败与遗漏风险。

使用安装后的脚本仍需在项目虚拟环境安装本仓库 `requirements.txt` 的依赖。若目标环境的 Skill 目录不同，以该环境配置为准。

## 验证

```bash
python3 -m unittest discover -s tests -v
```

测试不访问 X、不读取真实 Cookie；验证 UTC 窗口、去重、部分失败退出状态、进程锁、附件候选、文件落盘和重跑跳过。通过这些测试不等于已验证当前 X 登录或实时抓取。

## 文件说明

| 路径 | 用途 |
| --- | --- |
| `skills/x-twscrape/SKILL.md` | Agent 调用指引 |
| `skills/x-twscrape/references/implementation.md` | 当前项目方案、数据格式、已知限制与迁移说明 |
| `skills/x-twscrape/scripts/collect_x_tweets_90d.py` | 从原项目提取的采集器，支持任意显式账号列表 |
| `skills/x-twscrape/scripts/download_x_attachments.py` | 从原项目提取的图片和直接 PDF 下载器 |
| `skills/x-twscrape/scripts/x_watch.py` | 凭据导入、进程锁、轮询和状态查询 |

本包不包含 Investor Watcher 的网页、生产数据库、服务器配置、Codex 分析服务或 Telegram 凭据。抓取结果可由其他项目自行读取 JSONL（每行一个 JSON 对象）接入。
