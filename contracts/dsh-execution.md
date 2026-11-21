# DSH 执行契约

## 目的

把 DSH 固定为内部执行层。DSH 接收经过后端校验的执行请求，在指定 Agent Workspace 中运行工具，并返回可追踪的结果。

## 执行请求

执行请求至少包含：

- `execution_id`
- `agent_id`
- `workspace_id`
- `plan_step_id`
- 工具名称和结构化参数
- 工作目录
- 环境变量允许列表
- 超时时间和资源限制
- 网络和文件访问策略
- 关联审批和策略版本

当前 DSH Headless Adapter 首期能力收敛为 `dsh.task`：参数包含文本任务、可选的预期相对产物路径、基于事件流活跃度的空闲超时（默认 600 秒，覆盖模型长思考与整步输出）、可从持久化会话日志识别正常上下文压缩的独立压缩上限（默认 600 秒）以及默认 3,600 秒（最高可配置至 7,200 秒）的总安全上限，并在接近总上限（默认预留 300 秒）或出现阶段性进展后卡死时通过同会话 `--session-id` 触发软收尾写出已整理产物。若首轮虽返回完成但已有工具/步骤进展且因模型协议标记泄漏（如残缺 DSML 工具调用标签、未闭合 `<thinking>`）或遗漏预期报告/用户答复标记而触发 `artifact_validation` / `reply_contract` 校验失败，控制层会在剩余预算内通过同一 `--session-id` 自动执行一次断网局部契约补全收尾（复用 `work/outputs/` 已有产物写入缺失报告与标记），避免已生成的结果因单轮输出畸变被直接判失败。模型不能从请求参数扩大权限或改变工作区根路径。高风险请求必须引用同一 Work Item 且 scope 与 capability 完全匹配的已批准记录。

运行配置从仓库外的 `company-agent-runtime/runtime.env` 读取。`AGENT_MODEL_URL`、`AGENT_MODEL_API_KEY` 和 `AGENT_MODEL_NAME` 生成临时 Provider Route；受控 MCP 服务配置经 `COMPANY_AGENT_MCP_SERVERS` 与管理台 `/api/mcp-servers` 按 `admin` / `internal` / `common` 会话层级动态经 `dsh_plugins/controlled-mcp.mjs` 桥接至官方 `@deepseek-ai/dsh-mcp-client`。所有密钥（模型密钥、网闸密钥及受控 MCP 服务引用密钥）只按当前授权层级进入 DSH 主进程环境并纳入统一脱敏清单，绝不写入 Provider patch、Agent Workspace、SQLite、回执、日志或输出。WSL 运行前用 `scripts/sync-runtime-config.ps1` 同步到用户私有配置目录。

Headless 传输是 NDJSON：必须读取 `session`、检查所有 `tool_call`/`tool_result`、并收到 reason 为 `completed` 的 `final`。成功返回还要求每个声明的产物存在、是工作区内的普通文件、大小受限且不包含运行密钥。Reasoning/`thinking` 不保留为证据，并在写入结果输出时剥离原始 `<thinking>` 标签及泄漏的模型 DSML/工具分隔符标记。

当 QQ 任务需要把结论或生成文件交给用户时，最后一个执行请求会带有 `require_user_reply=true`。DSH 仍只在 Workspace 内工作，不发送 QQ 消息；它必须在声明的最终产物中写入唯一一对绑定当前 `execution_id` 的 `COMPANY_AGENT_FINAL_REPLY_BEGIN/END` 标记，标记内只能是用户可读结论且不超过 1,500 个汉字和标点，并可将生成的交付文件保存在 `work/outputs/<work_item_id>/` 下（可选通过 `COMPANY_AGENT_FILES_BEGIN/END` 显式列出）。即使遇到缺文件或工具阻塞，也必须通过 bash 写入包含上述说明的结果报告，避免无产物退出。Company Agent 在发布前重新校验标记、长度与非常规符号链接文件安全性，再分别将文本和文件送入 QQ Outbox 投递；暂存与产出文件随所属工作区保留可配置的留存期（默认 15 天），不随单步骤结束清除。

在 Ubuntu WSL2 开发运行时，DSH 的一次性 Bash 使用 Bubblewrap runner 叠加 `workspace-write`：隐藏 `/mnt`、`/run`、整个 WSL 用户目录和整个 Company Agent `--data-dir`，再将当前 Workspace 重新挂到原路径；自定义 `--config` 文件若位于其它可见目录，会被覆盖为 `/dev/null`，若位于当前 Workspace 则拒绝启动；并显式 `--unsetenv` 擦除所有模型、网闸和受控 MCP 密钥环境变量。Bash 使用独立网络命名空间，DSH 主进程仍可访问模型 Provider、受控搜索桥及授权层级的 MCP 服务。DSH 文件工具、持久 Bash、PowerShell、终端和工作目录切换工具关闭，避免绕过该 Shell 边界。缺少可执行 Bubblewrap，或 Workspace 位于受保护目录，执行在启动前失败。

## 执行结果

结果至少包含：

- `execution_id`
- `status`
- DSH Session ID、实际调用的工具和策略版本
- 开始和结束时间
- 退出码或错误类别
- 脱敏日志引用
- 带字节数和 SHA-256 的成果清单及文件引用
- 使用的工具和运行环境版本

Execution Request 启动前验证权限和参数。执行时，同一 Workspace 一次只持有一个运行租约。DSH 进程结束后先把结果回执原子写到工作区外的私有运行目录，再推进控制面结果。控制面写回失败时可以从回执恢复，不会重新调用工具；进程中断且没有回执时，任务会以 interrupted 失败，需检查其副作用并创建新的 Execution Request，不自动重放。

## 边界

- DSH 不决定业务权限和群聊发布。
- DSH 不直接写入任务最终状态。
- Bubblewrap 只包围 DSH 的一次性 Bash 子进程；DSH 和模型 API 调用在外部运行。WSL 用户目录与 Windows 挂载对该子进程不可见，当前 Workspace 可读写，其他 Linux 根目录以只读方式可见。
- Shell 网络已隔离，`/run` 中的宿主机 socket 不可见；DSH 主进程访问模型 Provider 不受影响。进程可见性、设备和资源配额仍未完整隔离，这不是容器或生产安全边界，不能据此连接生产资源。
- Windows/Linux 产品安装器必须准备平台等效的隔离 runner 并重新执行真实访问边界验收，不能把开发机的用户目录安装路径作为产品依赖。
- 每一次执行都绑定一个 Workspace 和一个可审计的执行请求。

