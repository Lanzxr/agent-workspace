# Company Agent 模块地图

更新日期：2026-11-16。树形图按“外部依赖、运行环境、项目自身”分类；状态依据当前代码和验收记录。

## 项目全景

```text
Company Agent 项目
├─ A. 外部平台与上游组件（被连接/调用的一方）
│  ├─ QQ 开放平台：提供群事件推送和消息发送 API
│  ├─ DSSuperAgent：提供消息观察、判断和计划能力
│  └─ DSH：提供任务执行能力
│
├─ B. 运行环境（软件运行在哪里）
│  ├─ 当前开发与验收
│  │  └─ Windows 电脑
│  │     └─ WSL2
│  │        └─ Linux 发行版/文件系统
│  │           └─ 运行 Company Agent 服务
│  └─ 未来部署目标
│     └─ Linux 主机
│        └─ 运行 Company Agent 服务
│
└─ C. Company Agent 自身模块（项目代码负责）
   ├─ 1. 渠道接入
   │  └─ QQ Channel Adapter：平台消息 ↔ 标准事件/待发送消息
   ├─ 2. 事件与控制
   │  ├─ Event Envelope：统一事件格式
   │  └─ Control Plane：状态、权限、审批、审计、发布决定
   ├─ 3. 判断与编排
   │  └─ DSSuperAgent Adapter / Workflow：传递事件，保存意图和计划
   ├─ 4. 任务执行
   │  ├─ Execution Dispatcher：派发获准任务并记录结果
   │  └─ DSH Adapter：将请求转换为 DSH 执行调用
   ├─ 5. 工作区管理
   │  └─ Workspace Manager：在 Linux 中创建和管理 Agent Workspace
   ├─ 6. 记忆、工具与数据源
   │  ├─ Memory：保存可供后续任务使用的信息
   │  └─ Tool/Data Source：提供受控的工具和数据读取能力
   └─ 7. 对外发布
      └─ Publication Decision / Outbox：决定并安排是否发送结果
```

**层级说明：** QQ、DSSuperAgent、DSH 是被连接的平台/组件；Windows、WSL2、Linux 是运行环境；树中 C 类才是 Company Agent 自己实现和维护的模块。WSL2 在 Windows 上提供 Linux 环境；Linux 主机部署时无需 WSL2。

**Agent Workspace：** 不是 WSL、Linux 或另一台虚拟机。它是 Workspace Manager 在 Linux 文件系统里为任务管理的目录和执行空间，用于存放输入、脚本、依赖、日志和产物。当前有路径、超时和输出等限制；完整 CPU、内存和进程隔离待完善。

## 模块状态

| 项目模块 | 状态 | 代码/证据 |
|---|---|---|
| QQ Channel Adapter | 已接入 Company Agent 正式事件链路；新群消息入库和真实外发尚待本轮部署后验收。 | `app/qq_channel.py`、`app/__main__.py`、`docs/07-step5-qq.md` |
| Event Envelope | 模型和契约已实现；渠道校验/幂等待适配器落实。 | `app/models.py`、`contracts/event-envelope.md` |
| Control Plane | SQLite 状态、审批、审计、发布和 Outbox 基础已实现。 | `app/control_plane.py`、`app/store.py` |
| DSSuperAgent Adapter / Workflow | Step 4 已验收；默认提出计划，不自动执行。 | `app/dssuperagent.py`、`app/assistant_workflow.py` |
| Execution Dispatcher / DSH Adapter | Step 3 真实 DSH 执行已验收。 | `app/execution.py`、`app/dsh.py` |
| Workspace Manager / Runtime | WSL2 核心执行与恢复已验收；完整系统资源配额待完善。 | `app/workspace.py`、`app/workspace_runtime.py` |
| Memory | 有模型和持久化基础；完整检索与使用流程待完善。 | `app/models.py`、`app/store.py` |
| Tools / Data Sources | `dsh.task` 和工作区内 Shell/Python 可用；独立工具目录、互联网检索及业务数据源待实现。 | 当前为架构扩展点 |
| Outbox 投递 | 已接入 QQ 发送 API、白名单检查、失败重试与人工解决未知发送状态；真实群发送未验收。 | `app/control_plane.py`、`app/qq_channel.py` |

## 现在到哪里

已验收：控制面基础、DSSuperAgent 工作流、DSH 执行、WSL2 Workspace。Step 5 的 QQ 适配器、标准事件入工作流及 Outbox 发送逻辑已实现，QQ 相关单元测试通过；运行配置读取、Token 获取和 Gateway URL 查询曾在真实配置下验证成功。

待完成的运行验收：从当前运行中的接收服务启动之后，在 QQ 群发一条新的低风险消息，并确认对应事件写入 Company Agent 事件库、工作流产生预期记录。真实群消息发送链路也尚未端到端验收；外发白名单为空时不会自动发送群消息，任务提案仍需人工审核。

详细契约见 `contracts/`；Step 3/4 记录见 `docs/04-step3-dsh.md`、`docs/05-step4-dssuperagent.md`。