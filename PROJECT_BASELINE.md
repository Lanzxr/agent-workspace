# 技术基线

版本：Architecture Reset 1.0  
日期：2026-11-08  
状态：架构重置后，等待按新契约实现

## 产品形态

这是一个通用自主助理平台，不是某个业务分析应用，也不是只等待明确指令的聊天窗口。

平台可以在授权范围内观察会话，识别是否存在值得处理的工作机会，静默形成计划并执行。执行完成后，系统独立判断是否需要向人或群发布。

## 逻辑分层

```text
Channel / Scheduler / System Event
                ↓
Event and Context Layer
                ↓
Company Agent Control Plane
                ├─ state, policy, memory, audit, outbox
                └─ DSSuperAgent Adapter
                        ↓
              DSSuperAgent
        silent observation and work orchestration
                        ↓
                  DSH Adapter
                        ↓
                       DSH
             internal execution layer
                        ↓
              Agent Workspace Manager
                        ↓
        isolated Linux workspace and artifacts
```

## 组件职责

| 组件 | 职责 | 不负责 |
|---|---|---|
| Company Agent Control Plane | 事件、状态、策略、权限、审批、记忆、审计、发布 | 复刻上游 Agent 内核 |
| DSSuperAgent | 静默观察、相关性判断、工作计划和执行编排 | 绕过后端权限和发布策略 |
| DSH | 会话执行、工具调用、结果回传 | 业务任务状态源 |
| Workspace Manager | 创建、恢复、隔离、限额、日志和成果管理 | 决定业务结论 |
| Channel Adapter | 入站标准化和出站投递 | 保存任务最终状态 |

## 通用工作链路

```text
Event
  → Context / Thread
  → DSSuperAgent 判断是否值得处理
  → Work Plan / Execution Intent
  → 后端权限与风险校验
  → DSH 在 Agent Workspace 执行
  → Evidence / Result / Memory
  → 后端 Publication Decision
  → Outbox
  → Channel Adapter
```

## 规范运行环境

Agent 的规范工作环境是可持久化的 Linux 环境。开发验证分层如下：

| 层 | 作用 | 不能替代 |
|---|---|---|
| Windows native-dev | 快速契约、单元和接口测试 | Linux 工作环境验收 |
| WSL2 dev | 完整工作环境、依赖、恢复和真实执行流程 | 安装包验收 |
| Linux/Docker | 部署镜像、资源隔离和运行时验收 | Windows 安装包验收 |
| Windows/Linux installer | 验证终端用户可以获得所需运行环境 | 开发期单元测试 |

## 第一条闭环

第一条闭环不是某个业务案例。它必须证明：经过授权的事件进入后，DSSuperAgent 能提出可执行工作，后端通过策略校验，DSH 在独立 Agent Workspace 中执行 Shell/Python，保存脚本、日志、成果和状态，重启后可以恢复，并由发布策略决定是否汇报。

