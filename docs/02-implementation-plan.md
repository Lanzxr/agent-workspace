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

当前进度：已实现 Event、Work Item、Work Plan、Execution Request、Execution Result、Publication Decision、Outbox 和 Audit 的 SQLite 最小闭环。已通过事件幂等、状态转换、执行结果回写和发布去重测试。

剩余工作：Conversation、Thread、Memory、审批策略和统一上游适配器仍待实现。

完成条件：事件可以产生工作意图，意图可以被后端接受、拒绝或等待审批。

## Step 2：实现 Agent Workspace

- 设计 Workspace Manager 接口。
- 在 WSL2 中实现 Linux Workspace 的创建、恢复、目录布局和资源限制。
- 接入 Shell、Python、文件和依赖操作。
- 验证日志、成果、状态和重启恢复。

完成条件：一次真实执行在独立 Workspace 中完成，容器或进程重启后仍可查询成果。

## Step 3：接入 DSH

- 实现 DSH Adapter。
- 把后端批准的 Execution Request 转换为 DSH 调用。
- 将 DSH 结果映射回 Execution Result。
- 明确真实 DSH 调用证据，不用 HTTP 兼容模型调用替代。

完成条件：DSH 能在指定 Workspace 执行一个真实任务并回传结果。

## Step 4：接入 DSSuperAgent

- 实现 DSSuperAgent Adapter。
- 传入事件和上下文，接收结构化工作意图与计划。
- 回传执行结果，支持会话恢复和失败重试。
- 验证静默观察、无明确指令形成工作意图和发布建议。

完成条件：完整链路经过 DSSuperAgent、控制面、DSH 和 Workspace。

## Step 5：接入渠道和主动发布

- 先保持 Web 和事件适配器作为开发入口。
- 接入第一个企业通讯渠道。
- 所有外发都经 Publication Decision 和 Outbox。
- 用多个领域夹具验证同一套通用链路。

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

