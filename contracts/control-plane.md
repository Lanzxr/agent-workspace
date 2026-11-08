# Control Plane Contract

控制面是 Company Agent 自己拥有的稳定边界。它不暴露 DSSuperAgent 或 DSH 的内部对象。

## 事件

`ingest_event` 以 `event_id` 做幂等。重复事件不会重复写入，也不会重复产生审计记录。

## 工作项

Work Item 的状态转换由后端验证：

```text
candidate → proposed → accepted → queued → running → completed
                                      ├────────────→ blocked
                                      └────────────→ cancelled
```

`blocked` 和 `failed` 可以回到 `accepted` 或 `queued`，用于人工确认和重试。终态不能被静默改写。

## 计划和执行

- Work Plan 只引用通用 Plan Step。
- Execution Request 必须绑定 Work Item、Plan Step、Workspace 和 Tool Capability。
- DSH 返回的结果通过 Execution Result 回写。
- 结果、成果引用和审计记录与执行请求处于同一持久化边界。

## 发布

Publication Decision 独立于 Work Item 完成。`suppress` 只保存决定。其他发布级别必须产生具有幂等键的 Outbox Message。

重复使用同一个幂等键只返回原有发布决定和 Outbox，不重复产生外发消息。

## 事务和恢复

状态变化、结果写入、Outbox 和审计在 SQLite 事务中提交。进程在事务中断时回滚。重启后以数据库状态为准恢复未完成工作。

