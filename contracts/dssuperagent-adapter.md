# DSSuperAgent 适配契约

## 目的

通过稳定的 HTTP 适配边界，将 Company Agent 接入 DSSuperAgent 的真实 Source、Trigger、Thread、Eval 和 Decision 流程。DSSuperAgent 提出意图；Company Agent 保存工作状态并最终负责权限、DSH 执行和发布。

适配器当前只接受 HTTP(S) loopback 地址。请求不携带 GitHub 身份、仓库或账号凭据。

## 输入

- 一个标准化 Company Agent Event。
- 同一会话中最多十条历史消息。
- 事件声明的能力名称。它们表示可用能力，不代表已获执行授权。

## 意图契约

每个事件被提交到 `POST /orchestrator/api/company-agent/events`，作为 Company Agent Source 上的稳定 Trigger，经过上游 Eval/Decision 流程。适配器只读取包含本次 Trigger ID 的 Decision。

当前结构化意图有两种动作：

- `observe`：保存事件和发布建议，不创建 Work Item。
- `propose_work`：提供标题、目标、完成条件和有序计划。每个步骤必须声明能力，且该能力同时存在于事件和 Company Agent 执行器配置中。

每个意图都包含发布建议（`suppress`、`private_nudge`、`group_update` 或 `urgent_escalation`）及原因。可选草稿正文只是内容，不代表发送授权。

对 Company Agent 事件，上游普通 Decision 必须保持为 `ignore` 或 `wouldAccept` 且不带 Ticket 确认。Ticket 或直接执行结果都属于协议错误。DSSuperAgent 不得创建 Ticket、启动 Worker、发送消息，也不得把事件数据当成权限。

## 控制面映射

- `observe` 保存为没有 Work Item 的 `AssistantRun`。
- `propose_work` 创建一个候选并转为待处理 Work Item，ID 根据源事件确定。原始 Decision、计划意图、发布建议和不可变策略版本保存在 Company Agent 控制库；每次 AssistantRun 状态变化都会审计 Decision、Trigger、Work Item、Plan 和 Execution 的引用。
- 默认策略不自动授权任何能力。后端 allowlist 可以授权低风险能力；其他提案必须经过现有审批流程。`ExecutionDispatcher` 和控制面仍会校验每个 Execution Request。
- 发布建议保存在 `AssistantRun`。Step 4 不创建 Outbox，也不发送渠道消息；渠道投递由后续步骤接入。
- DSH 结果保存为 Execution Result，并以稳定事件 ID 作为 `company-agent.execution-result` 事件回到同一 DSSuperAgent 会话。

## 幂等与恢复

事件、Work Item、Plan、Execution 和结果事件使用稳定 ID。重复事件返回已保存的运行状态。已完成的 DSH 执行通过持久化回执恢复，不重放工具调用。

缺少上游结果反馈时，可以基于已保存的 DSH 结果重试补投。DSH 失败、超时或中断会保存为终态；相同 Execution ID 不会被自动重放，以避免重复副作用。新的执行恢复流程必须另行检查执行痕迹并重新授权。

上游不可用、超时、无效 JSON、Decision 缺失、能力未知和违规 Ticket 结果都必须明确失败分类。Mock 响应不代表 DSSuperAgent 真实评估通过。

## 后续扩展

当前适配不处理记忆提案审批、外部数据源、多种执行器、实时渠道投递或群聊发布策略。后续通过控制面契约扩展，不直接暴露 DSSuperAgent 内部实现。
