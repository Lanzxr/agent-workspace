# 重置后的实施步骤

## Step 0：固定架构和上游事实

- 核验 DSSuperAgent 的真实源码、启动方式、输入输出和持久化。
- 核验 DSH 的真实安装方式、会话接口、工具接口和 Workspace 依赖。
- 记录许可证、版本、网络依赖和不可用能力。
- 不把猜测接口写成实现。

完成条件：两套上游各自有事实记录和适配边界。

## Step 1：建立通用控制面契约

- 实现 Event、Conversation、Thread、Work Item、Work Plan、Execution Request、Result、Memory 和 Publication Decision 的最小模型。
- 建立幂等、状态转换、审计和 Outbox。
- 先用确定性假适配器验证状态机，不宣称上游接入完成。

当前进度：已实现 Event、Conversation、Thread、Memory、Work Item、Work Plan、Execution Request、Execution Result、Publication Decision、Outbox 和 Audit 的 SQLite 最小闭环。已实现提案、接受、拒绝、审批等待、高风险执行审批引用和中断执行恢复。确定性假适配器已验证事件到提案的边界。

剩余工作：完整权限策略和 DSSuperAgent Adapter 仍待实现；DSH 执行适配已在 Step 3 完成。

完成条件：事件可以产生工作意图，意图可以被后端接受、拒绝或等待审批。

## Step 2：实现 Agent Workspace

- 设计 Workspace Manager 接口。
- 在 WSL2 中实现 Linux Workspace 的创建、恢复、目录布局和资源限制。
- 接入 Shell、Python、文件和依赖操作。
- 验证日志、成果、状态和重启恢复。

当前进度：已实现 `WorkspaceManager` 和 `WorkspaceRuntime` 的第一条可验收闭环。它们负责 Workspace 创建、固定目录结构、manifest 和依赖清单持久化、运行中状态恢复、Shell/Python 执行、超时和输出限制、成果和日志写入、路径越界检查，以及 Linux 目录和文件权限收紧。Windows 本地测试和 Ubuntu 24.04 WSL2 测试均通过。WSL2 可重复执行 `bash scripts/verify-workspace-wsl.sh`，脚本还会用两个独立 Python 进程验证执行结果和成果可恢复读取。

剩余工作：补充 Linux 级别的 CPU、内存和进程资源策略。执行租约、WorkspaceRuntime 与 DSH Adapter 已在 Step 3 接入控制面。

完成条件：一次真实执行在独立 Workspace 中完成，容器或进程重启后仍可查询成果。

## Step 3：接入 DSH

- 实现 DSH Adapter。
- 把后端批准的 Execution Request 转换为 DSH 调用。
- 将 DSH 结果映射回 Execution Result。
- 明确真实 DSH 调用证据，不用 HTTP 兼容模型调用替代。

当前进度：已实现 DSH Headless Adapter、配置文件到 Provider Route 的生成、控制面派发和结果回写、工作区独占租约、持久执行回执、超时/协议/产物失败分类。Ubuntu 24.04 WSL2 使用真实 DSH 0.2.2-rc.1 和企业模型网关通过业务任务、Bubblewrap 文件/网络边界、产物哈希、重启恢复和重复派发验收。真实任务只调用一次性 Bash，无法读取 WSL/Windows 模型配置、其它 Company Agent 运行数据或 Docker socket，且不能直接出网；DSH 主进程仍可调用模型 Provider。测试命令和证据见 [`04-step3-dsh.md`](04-step3-dsh.md)。

剩余工作：更广泛的进程/设备隔离、CPU/内存/进程配额、正式安装包和生产权限策略不属于本步骤能力，需后续运行时和安装器步骤解决。

完成条件：DSH 能在指定 Workspace 执行一个真实任务并回传结果。

## Step 4：接入 DSSuperAgent

- 实现 DSSuperAgent Adapter。
- 传入事件和上下文，接收结构化工作意图与计划。
- 回传执行结果，支持会话恢复和失败重试。
- 验证静默观察、无明确指令形成工作意图和发布建议。

当前进度：已实现 loopback-only DSSuperAgent Adapter 和 `DssuperAgentWorkflow`。标准事件经上游 Source/Trigger/Thread/Eval/Decision 后映射为持久 AssistantRun、Work Item 和 Plan；默认策略只提出、不自动执行，后端显式 allowlist 才能让低风险能力进入 DSH。高风险执行仍经审批，执行结果作为稳定 ID 的事件回传上游，发布建议只保存、不外发。上游桥接补丁固定基线并纳入 [`patches/dssuperagent-company-agent-step4.patch`](../patches/dssuperagent-company-agent-step4.patch)，真实运行验收证据见 [`05-step4-dssuperagent.md`](05-step4-dssuperagent.md)。

完成条件：完整链路经过 DSSuperAgent、控制面、DSH 和 Workspace。

Step 5 已完成 QQ 首个渠道接入和 Outbox 投递闭环。剩余工作是部署后的真实 QQ 验收；更广泛的渠道支持与策略管理界面属于后续扩展。安装包和 Linux 级资源配额由后续步骤处理。

## Step 5：接入渠道和主动发布

- 已接入 QQ Bot Gateway 和群消息 API，直接连接 Company Agent QQ Channel Adapter。
- 群消息映射为标准事件并进入 DSSuperAgent 工作流；默认只形成提案，不授权 DSH 执行。
- 外发建议经显式 QQ 发布白名单、Publication Decision 和 Outbox；未配置白名单时不发送群消息。
- 本地测试覆盖事件标准化、幂等、白名单、Outbox 失败重试与 Gateway 控制帧。真实 QQ Gateway 和真实群发送仍需部署验收。

详细配置和运行方式见 [`07-step5-qq.md`](07-step5-qq.md)。

完成条件：渠道只负责传输，任务状态和发布决定仍由控制面保存。

## Step 6：制作安装包

- Windows 安装器自动检测和准备 WSL2、Docker 或替代运行依赖。
- Linux 安装器自动检测和准备 Docker 或原生 Linux 运行依赖。
- 安装后自动创建 Workspace、运行健康检查、登记版本和输出诊断信息。
- 升级、卸载、恢复和离线失败都可诊断。

完成条件：终端用户不需要手工安装内部依赖即可启动助理。

## 验收规则

- 单元和契约测试不能代替真实上游验收。
- HTTP 兼容模型连接不能标记 DSH 已通过。
- 一次命令成功不能标记 Workspace 重启恢复已通过。
- 业务案例只能作为夹具，不能改变核心数据模型。
