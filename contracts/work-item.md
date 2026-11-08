# Work Item Contract

Work Item 是通用工作事项。它不携带任何特定行业指标。

必填字段：

- `work_item_id`
- `title`
- `objective`
- `status`
- `source_event_ids`
- `owner_ref`
- `completion_conditions`
- `risk_class`
- `created_at`
- `updated_at`

推荐状态：`candidate`、`proposed`、`accepted`、`queued`、`running`、`blocked`、`completed`、`failed`、`cancelled`、`needs_approval`、`rejected`。

Work Item 的领域内容通过 Data Source、Tool Capability 和 Policy Pack 表达。核心状态机不根据领域名称增加分支。

