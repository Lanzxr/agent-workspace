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

当前 DSH Headless Adapter 首期能力收敛为 `dsh.task`：参数包含文本任务、可选的预期相对产物路径和最长 300 秒的执行超时。模型不能从请求参数扩大权限或改变工作区根路径。高风险请求必须引用同一 Work Item 且 scope 与 capability 完全匹配的已批准记录。

运行配置从仓库外的 `company-agent-runtime/runtime.env` 读取。`AGENT_MODEL_URL`、`AGENT_MODEL_API_KEY` 和 `AGENT_MODEL_NAME` 生成临时 Provider Route；密钥只进入 DSH 进程环境，不写入 Provider patch、Agent Workspace、SQLite、回执、日志或输出。WSL 运行前用 `scripts/sync-runtime-config.ps1` 同步到用户私有配置目录。

Headless 传输是 NDJSON：必须读取 `session`、检查所有 `tool_call`/`tool_result`、并收到 reason 为 `completed` 的 `final`。成功返回还要求每个声明的产物存在、是工作区内的普通文件、大小受限且不包含运行密钥。Reasoning/`thinking` 不保留为证据。

在 Ubuntu WSL2 开发运行时，DSH 的一次性 Bash 使用 Bubblewrap runner 叠加 `workspace-write`：隐藏 `/mnt`、`/run`、整个 WSL 用户目录和整个 Company Agent `--data-dir`，再将当前 Workspace 重新挂到原路径；自定义 `--config` 文件若位于其它可见目录，会被覆盖为 `/dev/null`，若位于当前 Workspace 则拒绝启动。Bash 使用独立网络命名空间，DSH 主进程仍可访问模型 Provider。DSH 文件工具、持久 Bash、PowerShell、终端和工作目录切换工具关闭，避免绕过该 Shell 边界。缺少可执行 Bubblewrap，或 Workspace 位于受保护目录，执行在启动前失败。

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

