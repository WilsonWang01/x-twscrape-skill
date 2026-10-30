# Agent 接入说明

## 连接信息

| 项目 | 值 |
| --- | --- |
| 服务名称（建议） | `investor-watcher` |
| 公网地址 | `https://investorwatcher.watch/mcp` |
| 传输 | Streamable HTTP，不是旧版 SSE 入口 |
| 认证 | `Authorization: Bearer <独立 MCP 访问凭证>` |
| 凭证来源 | 由服务所有者通过私密渠道提供，不随 Skill 分发 |

不使用 X Cookie、AWS 密钥或网站管理员密码。本服务没有 OAuth 登录流程，不能用 `codex mcp login` 获得凭证。客户端必须支持 Bearer/自定义请求头；只支持 OAuth 的客户端不能据此保证可用。

Skill 与 MCP 连接是两件事：安装 Skill 只提供工作指引。先让宿主客户端完成连接并能发现工具，Agent 才能执行查询。`agents/openai.yaml` 只声明依赖，不包含凭证，也不保证自动注册连接。

## Codex 配置示例

把以下表合并到目标宿主的 `~/.codex/config.toml`，或受信任项目的 `.codex/config.toml`。先检查是否已有同名表，只修改对应字段，不能覆盖整个配置文件或删除其他 MCP。

```toml
[mcp_servers.investor-watcher]
url = "https://investorwatcher.watch/mcp"
bearer_token_env_var = "INVESTOR_WATCHER_MCP_TOKEN"
```

`INVESTOR_WATCHER_MCP_TOKEN` 的值是裸凭证，不含 `Bearer ` 前缀。让实际运行 MCP 客户端的进程从安全环境或受保护文件获取它。不要把真实值写进 Skill、仓库或命令参数，也不要打印环境变量。

已有受保护凭证文件时，可在 Bash/Zsh 中从文件加载后启动 CLI；先将路径改为实际私密文件路径，不要把密钥内容直接粘贴进命令：

```bash
TOKEN_FILE="/安全目录/investor-watcher.token"
INVESTOR_WATCHER_MCP_TOKEN="$(< "$TOKEN_FILE")" codex
```

不要在开启 `set -x` 的调试终端执行上述命令。此方式不会为已运行的桌面应用设置环境变量；桌面/远程宿主需要使用其支持的凭证注入方式。不要为此自动重启用户应用或修改启动服务。

运行 `codex mcp list` 或在客户端工具列表检查配置；这只能证明配置存在，不能代替下面的真实工具调用。需要重新加载连接时，按宿主的重连/重启方式处理。

## 其他 Agent 的配置示意

仅供支持这种配置格式的客户端参考，不是所有 Agent 都能直接导入：

```json
{
  "mcpServers": {
    "investor-watcher": {
      "type": "http",
      "url": "https://investorwatcher.watch/mcp",
      "headers": {"Authorization": "Bearer <由安全凭证存储注入>"}
    }
  }
}
```

按目标客户端的官方文档把占位值接到其凭证存储。不要假设 `${ENV_VAR}` 会自动展开；若客户端只接受明文私密配置，该文件不得入库或加入分发包。

## 安装 Skill

将完整的 `investor-watcher-mcp` 目录放入目标 Agent 的 Skill 目录，保留 `SKILL.md`、`agents/` 和 `references/`。Codex 当前支持用户级 `~/.agents/skills/investor-watcher-mcp/` 和项目级 `.agents/skills/investor-watcher-mcp/`。安装前检查目标是否存在，不覆盖已有版本。其他 Agent 使用其自己的 Skill 路径；不支持 Skill 发现的 Agent 可按需读取 `SKILL.md`，但仍须单独配置 MCP。

安装后可发出：

```text
使用 $investor-watcher-mcp 查询当前关注账号，并汇总北京时间今天的看多喊单，附原文链接和数据最新时间。
```

## 验证与故障处理

1. 通过 MCP 客户端初始化连接并列出工具，应发现 `list_accounts`、`search_posts`、`get_post`、`list_attachments`、`get_collection_status`。若名称带前缀，使用宿主实际暴露的名称。
2. 调用 `get_collection_status` 和 `list_accounts`，确认收到实际返回，不硬编码账号数或帖子数。
3. 调用 `search_posts`，参数 `{"limit":1}`；有结果时用返回的字符串 `post_id` 调用 `get_post`，检查 `found`。只看到配置或 HTTP 200 不能证明完整协议已连通。
4. 用户需要附件时，再检查清单与实际下载。无缓存、超限或 404 应说明附件未能取得，不把原始链接当作已下载。

| 现象 | 处理 |
| --- | --- |
| 浏览器直接打开提示需要凭证 / HTTP 401 | 未带凭证时正常，不是登录页面；配置 Bearer 后再用 MCP 客户端验证 |
| 客户端带凭证仍 401 | 检查宿主是否收到裸凭证、请求头是否重复或前缀重复；不要自动换凭证、登录 X 或反复重试 |
| HTTP 403 | 可能是 Host/Origin 不允许；使用规定 HTTPS 域名，必要时请管理员核对客户端来源；不伪造来源绕过限制 |
| HTTP 429 | 遵守 `Retry-After`；无该头时至少等 60 秒，重试一次，仍失败则报告 |
| TLS/证书错误或超时 | 保留校验，核对 DNS、证书、代理和客户端网络；不使用 `verify=false` / `curl -k` |
| 工具调用 `isError` 或 SDK 的 `is_error` 为真 | 读取安全错误信息并报告，不当作空结果；不会总是表现成 HTTP 错误 |
| 查不到帖子 / 数据时间较旧 | 仅说明库中未找到或数据延迟；MCP 不会主动抓取，检查 `get_collection_status` 后交由服务维护方处理 |

查询读取已存数据，不调用 X、分析模型或 Telegram。大批量查询/下载仍消耗服务资源与流量，不能保证零费用；未经确认不做全库导出、持续轮询或大批量下载。

Codex 配置与技能发现位置依据：[官方 MCP 文档](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)、[官方 Skill 文档](https://learn.chatgpt.com/docs/build-skills)，核对日期 2026-10-30。客户端后续版本可能变化，以官方文档和实际工具发现为准。
