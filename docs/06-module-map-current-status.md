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
   ├─ 7. 对外发布
   │  └─ Publication Decision / Outbox：决定并安排是否发送结果
   ├─ 8. 用户身份与附件
   │  ├─ Identity Registry：平台账号绑定内部用户
   │  └─ Attachment Ingestor：校验、下载附件并提交资料库或提取文本
   ├─ 9. 长期资料库
   │  ├─ Knowledge Service：来源、权限、哈希去重、查询授权和审计
   │  ├─ AnythingLLM Adapter：索引副本上传、解析、向量检索
   │  └─ Knowledge Delivery：经权限复核后把原件交付到 DSH 执行目录
   ├─ 9. 定时续接
   │  └─ Scheduler / Resume Handler：持久化到期时间与上下文，到期后续接来源会话
   └─ 10. 运维中控台
      └─ Admin API / Web：管理身份、定时任务、联网开关和运行状态
```

**层级说明：** QQ、DSSuperAgent、DSH 是被连接的平台/组件；Windows、WSL2、Linux 是运行环境；树中 C 类才是 Company Agent 自己实现和维护的模块。WSL2 在 Windows 上提供 Linux 环境；Linux 主机部署时无需 WSL2。

**Agent Workspace：** 不是 WSL、Linux 或另一台虚拟机。它是 Workspace Manager 在 Linux 文件系统里为任务管理的目录和执行空间，用于存放输入、脚本、依赖、日志和产物。当前有路径、超时和输出等限制；完整 CPU、内存和进程隔离待完善。

## 模块状态

| 项目模块 | 状态 | 代码/证据 |
|---|---|---|
| QQ Channel Adapter | 已接入正式事件链路；先保存附件占位和原件，再后台 OCR/AnythingLLM 入库；Agent 明确选件后由 Company Agent 复核并交付 DSH。真实 QQ 群消息和 CDN 下载仍需部署验收。 | `app/qq_channel.py`、`app/attachments.py`、`docs/07-step5-qq.md` |
| Knowledge Base | AnythingLLM 适配、内容哈希去重、群/私聊/管理员权限、DSSuperAgent 短期检索授权、DSH 原件交付已实现；真实 AnythingLLM 服务验收仍需部署执行。 | `app/knowledge/`、`docs/09-knowledge-base.md` |
| Event Envelope | 模型和契约已实现；渠道校验/幂等待适配器落实。 | `app/models.py`、`contracts/event-envelope.md` |
| Control Plane | SQLite 状态、审批、审计、发布和 Outbox 基础已实现。 | `app/control_plane.py`、`app/store.py` |
| DSSuperAgent Adapter / Workflow | Step 4 已验收；默认提出计划，不自动执行。 | `app/dssuperagent.py`、`app/assistant_workflow.py` |
| Execution Dispatcher / DSH Adapter | Step 3 真实 DSH 执行已验收。 | `app/execution.py`、`app/dsh.py` |
| Workspace Manager / Runtime | WSL2 核心执行与恢复已验收；完整系统资源配额待完善。 | `app/workspace.py`、`app/workspace_runtime.py` |
| Memory | 有模型和持久化基础；完整检索与使用流程待完善。 | `app/models.py`、`app/store.py` |
| Identity Registry | 已支持内部用户创建、平台 ID 绑定/解绑、群级及全局解析；真实 QQ 账号绑定待部署操作验收。 | `app/identity.py`、`app/store.py` |
| Attachment Ingestor | 支持稳定附件 ID、原件先保存、状态恢复和后台 OCR/AnythingLLM 入库；真实 QQ 下载/解析仍需部署验收。 | `app/attachments.py`、`docs/08-management-modules.md` |
| Scheduler / Resume Handler | UTC 持久化、时区转换、到期轮询、失败重试和服务重启恢复已接线；唤醒交给 DSSuperAgent 判断，不自动授权执行。 | `app/scheduler.py`、`app/scheduled_resume.py` |
| Tools / Data Sources | `dsh.task` 和 Workspace 内 Shell/Python 已有；DSH 官方 `tool-web` 现由控制台开关控制。真实联网查询仍需在部署环境验收。 | `app/dsh.py`、`app/runtime_config.py` |
| Admin Console | 已提供 loopback 认证控制台及用户、绑定、日程、联网设置、状态、用量和审计 API。 | `app/admin_api.py`、`web/index.html` |
| Outbox 投递 | 已接入 QQ 发送 API、白名单检查、失败重试与人工解决未知发送状态；真实群发送未验收。 | `app/control_plane.py`、`app/qq_channel.py` |

## 现在到哪里

已验收：控制面基础、DSSuperAgent 工作流、DSH 执行、WSL2 Workspace。Step 5 的 QQ 适配器、标准事件入工作流及 Outbox 发送逻辑已实现，QQ 相关单元测试通过；运行配置读取、Token 获取和 Gateway URL 查询曾在真实配置下验证成功。

待完成的运行验收：部署更新并配置 AnythingLLM 后，使用真实 QQ 群和私聊测试消息、附件入库及检索、用户绑定、定时续接和联网开关；外发仍须通过白名单和 Outbox，定时唤醒也只产生 Agent 判断，不会绕过现有审批自动执行 DSH。资料库部署和旧 RAGFlow 配置迁移见 `docs/09-knowledge-base.md`。

详细契约见 `contracts/`；Step 3/4 记录见 `docs/04-step3-dsh.md`、`docs/05-step4-dssuperagent.md`。
