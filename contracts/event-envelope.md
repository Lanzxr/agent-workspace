# Event Envelope

所有外部输入先转换成统一事件。事件只描述发生了什么，不直接表达执行许可。

必填字段：

- `event_id`
- `source_type`
- `source_id`
- `conversation_id`
- `actor_id`
- `occurred_at`
- `content`
- `attachments`
- `capabilities`

入站适配器负责幂等和来源校验。控制面负责上下文关联、权限判断和后续状态变化。

