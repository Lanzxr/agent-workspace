# Company Agent

Company Agent 是一个通用的公司内部自主助理平台。它观察经过授权的事件和会话，在需要时静默开展工作，调用 Agent 工作环境完成任务，保存证据和结果，并依据发布策略向合适的渠道汇报。

## 项目定义

本项目由四个核心部分组成：

1. **通用助理平台**负责事件接入、上下文、任务状态、策略、审计、记忆和渠道发布。
2. **DSSuperAgent**作为静默观察、工作判断、计划组织和执行编排的主要地基。项目通过适配器接入它，不复制它的内部实现。
3. **DSH**作为内部执行层，负责会话执行、工具调用和结果回传。它不是项目的业务编排层。
4. **Agent 工作环境**补齐 DSSuperAgent 或 DSH 运行所需的隔离 Linux 环境、文件、依赖、权限、日志、恢复和生命周期能力。

业务领域通过数据源、工具能力和策略注册接入。业务领域名称、指标和流程不写入通用编排器。

## 设计原则

- Agent 可以在没有明确 @ 或命令时观察并准备工作，但分析、执行和发布是独立的后端决策。
- 模型和上游 Agent 只能提出计划、执行意图和结果解释，权限、审批、发布和状态转换由后端验证。
- 每个 Agent 或工作运行都有可恢复的工作环境，工作文件、成果、日志和状态分开保存。
- 开发阶段可以使用 Windows native-dev、WSL2 和 Docker 做分层验证。最终安装包负责在 Windows 或 Linux 上准备依赖，用户不需要手工理解内部运行时。
- 全部实现不接入 Codex，也不保留第二套专业编程执行器。
- 真实上游接口、模型和渠道必须用真实运行证据验收。模拟器只用于契约测试。

## 目录（四大文档分类）

### 一、使用手册
- [Company Agent 使用手册](docs/user-manual.md)（角色权限分级、QQ 群/私聊全部指令、Tool Forge 对话造工具与审批、Web 控制台功能详解）

### 二、部署手册
- [Company Agent 部署手册](docs/deployment-manual.md)（WSL2 与独立 Linux 服务器/云主机部署全流程、可配置部署用户、Docker 搜索与资料库、SSH 远程安全访问、升级与回滚）

### 三、导入 MCP 手册
- [导入 MCP 与 Tool Forge 扩展手册](docs/mcp-manual.md)（能力选型、导入外部 `stdio`/`streamable-http`/`wsl-host` MCP、Tool Forge 对话锻造、密钥托管及 DSH JSON Schema 规范）

### 四、技术文档
- **项目基线与领域建模**：[领域语言](CONTEXT.md) · [项目边界](PROJECT_BOUNDARY.md) · [技术基线](PROJECT_BASELINE.md) · [技术决策](DECISIONS.md)
- **架构设计与模块说明**：[架构说明](docs/01-architecture.md) · [实施步骤](docs/02-implementation-plan.md) · [模块状态映射](docs/06-module-map-current-status.md) · [管理能力与模块说明](docs/08-management-modules.md) · [上游事实核验表](docs/00-upstream-fact-sheet.md) · [Step 0 调研报告](docs/step0-upstream-research-agent.md)
- **决策记录（ADR）**：[自定义模型 Provider Route](docs/adr/0002-custom-model-provider-route.md) · [GitHub Source 可选边界](docs/adr/0003-github-source-is-optional.md) · [重置说明](docs/03-reset-note.md)
- **系统契约与补丁**：[工作环境契约](contracts/agent-workspace.md) · [控制面契约](contracts/control-plane.md) · [DSSuperAgent 适配契约](contracts/dssuperagent-adapter.md) · [DSH 执行契约](contracts/dsh-execution.md) · [DSSuperAgent Step 4 桥接补丁](patches/dssuperagent-company-agent-step4.patch) · [DSSuperAgent 资料库检索补丁](patches/dssuperagent-knowledge-bridge.patch)
- **子系统实施与验收记录**：[Step 3 DSH 接入与验收](docs/04-step3-dsh.md) · [Step 4 DSSuperAgent 接入与验收](docs/05-step4-dssuperagent.md) · [Step 5 QQ 渠道接入](docs/07-step5-qq.md) · [受控 Web 检索](docs/09-controlled-web-search.md) · [长期资料库与 AnythingLLM 接入](docs/09-knowledge-base.md) · [原始需求归档](source/README.md)

## 长期资料库

QQ 长期资料库由 Company Agent 接入自托管 AnythingLLM。Company Agent 保管原件并在服务端校验群聊、私聊和管理员权限；DSSuperAgent 通过受控查询接口取得证据，DSH 不连接资料库。部署和迁移说明见 [AnythingLLM 接入说明](docs/09-knowledge-base.md)。500 MiB 是应用与代理目标限制，AnythingLLM 的真实解析能力仍须在部署环境通过大文件联调验收。

