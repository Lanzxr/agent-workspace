# DSSuperAgent 适配契约

## 目的

把 DSSuperAgent 作为静默观察、工作判断、计划组织和执行编排的上游地基接入 Company Agent。业务核心只依赖本契约，不直接依赖 DSSuperAgent 的内部模块、数据库或提示词。

## 输入

- 标准化 Event
- Conversation 和 Thread 上下文
- 当前 Work Item、Memory 和 Policy 摘要
- 可用 Data Source、Tool Capability 和 Workspace 能力清单

## 输出

DSSuperAgent 可以提出以下意图：

- `observe`：保存上下文，不开始工作
- `propose_work`：提出工作项和完成条件
- `propose_plan`：提出有序执行计划
- `request_execution`：提出调用 DSH 的执行意图
- `propose_memory`：提出需要保存的事实或规则
- `propose_publication`：提出结果发布建议

所有输出都必须带有输入事件引用、说明、置信度和结构化字段。输出只是提案，不能直接改变状态、发送消息或扩大权限。

## 生命周期

适配器必须支持：

1. 创建或恢复 Agent 会话。
2. 传入新事件和上下文。
3. 获取结构化意图或工作计划。
4. 将 DSH 结果回传给 DSSuperAgent。
5. 在重启后通过稳定 ID 恢复关联。

## 失败处理

上游不可用、输出无法解析或版本不兼容时，适配器必须返回可分类的错误。后端可以重试或记录阻塞，但不能把规则模拟结果标记成 DSSuperAgent 已完成。

