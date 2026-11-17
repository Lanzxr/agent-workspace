# 管理能力与模块说明

更新日期：2026-11-16

## 模块边界

| 模块 | 责任 | 主要入口 |
|---|---|---|
| QQ Channel | 接收群消息、映射内部用户、读取附件并转成事件；发送仍经过发布决定和 Outbox。 | `app/qq_channel.py` |
| 附件读取 | 安全下载并提取文本；二进制不落盘，提取状态和文本摘要写入附件记录。 | `app/attachments.py` |
| 用户身份 | 管理内部用户及 QQ/其他平台外部 ID 绑定；群级绑定优先，全局 `*` 绑定作为回退。 | `app/identity.py` |
| 定时任务 | 将用户本地时间换算为 UTC，持久化来源事件、工作项、续接提示、上下文和回复目标；重启后恢复未完成唤醒。 | `app/scheduler.py`、`app/scheduled_resume.py` |
| 联网检索开关 | 控制库分别保存 DSSuperAgent 和 DSH 的联网权限。DSSuperAgent 每次搜索/读网页前经过受控网关检查；DSH 仍在创建执行配置时读取自己的开关，操作官方 `tool-web` 插件，不改 DSH 核心。 | `app/web_gate.py`、`app/admin_api.py`、`app/runtime_config.py`、`app/dsh.py` |
| 中控台 | 提供本地认证网页/API，管理用户绑定、日程和联网设置，并查看状态、空间用量、审计记录及指定执行的 stdout/stderr。 | `app/admin_api.py`、`web/index.html` |

## 附件规则

支持 TXT、Markdown、CSV、JSON、HTML、DOCX、XLSX 和 PDF。下载仅允许标准 HTTPS、拒绝重定向、限制 12 MiB 下载体积；提取内容最多 40,000 字符。Office 压缩包另有限制。PDF 需要安装可选依赖 `pip install .[attachments]`；未安装或格式不可读时，记录失败原因，消息仍会进入判断链路，但不会包含文件正文。PNG/JPG/JPEG 和扫描版 PDF 交给 AnythingLLM OCR；OCR 未完成时保留原件和占位状态，失败记录为 `failed`，不会误报为已完成。

## 用户绑定

QQ 群成员原始 ID 保存在 `external_actor_id`；Agent 收到的是 Company Agent 的 `user_id`。未绑定成员会得到带平台、机器人、群和成员 ID 的隔离身份，不会误合并。创建绑定前需先在中控台创建内部用户。将范围设为 `*` 表示该 QQ 账号内的全局绑定；指定群 ID 则仅在该群生效。

## 定时任务与恢复

创建定时任务需要已入库的来源事件 ID、关联工作项 ID、续接提示、到期时间和 IANA 时区。无时区偏移的时间按所选时区转换为 UTC；夏令时重复或不存在的本地时间必须显式提供偏移。默认保存来源消息和附件提取文字的前 16,000 字符，并可另存摘要；大于该长度会标记截断。控制台服务每 5 秒检查到期任务，创建稳定 ID 的续接事件，并沿用来源会话。若提供 QQ 群 OpenID，生成的发布结果仍需通过 QQ 发布白名单和 Outbox。

调度只是唤醒并把上下文交回 Agent 判断，不自动授予 DSH 执行或发布权限。服务重启会重新排队状态为 `running` 的任务；续接事件 ID 稳定，因此重复处理会由现有幂等状态拦截。失败任务可通过 API 重试。

## 启动中控台

先在私有运行配置中设置 QQ 凭据；管理令牌至少 24 个字符。配置 `runtime.env.example` 中的 `COMPANY_AGENT_ADMIN_TOKEN`，随后从项目目录运行：

```powershell
$env:COMPANY_AGENT_ADMIN_TOKEN = "<随机生成的至少24字符令牌>"
py -m app --data-dir "$HOME/.local/share/company-agent" admin-serve --config "$HOME/.config/company-agent/runtime.env"
```

打开 `http://127.0.0.1:8765` 并输入令牌。服务默认只监听 loopback；远程管理应放在带 TLS 和身份认证的反向代理后，不要直接绑定公网地址。管理网页使用 `sessionStorage` 保存令牌。

## 联网检索

默认关闭，分成两个开关：

- `DSSuperAgent 联网检索`：控制 `orchestrator_web_search` 和 `orchestrator_web_fetch`。每次调用先请求 Company Agent 的 `/internal/web/authorize`，开关关闭时返回 403，开关打开后才调用 DSH 官方 `ctx.web` 能力。切换后不需要重启 DSSuperAgent，下一次调用立即使用新状态。
- `DSH 任务联网`：控制 Company Agent 启动的 DSH 执行进程是否加载官方 `tool-web`。它只影响新建的 DSH 执行，已经运行的进程不热更新。

这样可以只允许 DSSuperAgent 查资料，也可以只允许获准的 DSH 任务联网。两层权限互不替代，资料库检索也不受这两个开关影响。

受控网关默认监听 `127.0.0.1:8767`，配置项是 `COMPANY_AGENT_WEB_GATE_HOST`、`COMPANY_AGENT_WEB_GATE_PORT`、`COMPANY_AGENT_WEB_GATE_API_KEY`。DSSuperAgent 进程需要继承 `COMPANY_AGENT_WEB_GATE_URL`（默认 `http://127.0.0.1:8767/internal/web/authorize`）和同一个 API Key（非 loopback 部署必须设置至少 32 个字符）。搜索结果由 DSH 官方 Web seam 返回，包含来源 URL；Company Agent 只负责权限、审计和即时开关，不改 DSH 本体。

## 验收范围

自动化测试覆盖身份解析、时区换算和夏令时、调度状态恢复/重试、文本/CSV/附件记录、联网开关配置及管理 API 认证。真实 QQ CDN 文件、PDF 可选依赖、生产 DSH 搜索调用、远程部署访问和真实群组续接消息需要部署后用实际凭据验收。
