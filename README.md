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

## 目录

- [项目边界](PROJECT_BOUNDARY.md)
- [技术基线](PROJECT_BASELINE.md)
- [技术决策](DECISIONS.md)
- [架构说明](docs/01-architecture.md)
- [上游事实核验表](docs/00-upstream-fact-sheet.md)
- [Step 0 上游事实核验报告](docs/step0-upstream-research-agent.md)
- [工作环境契约](contracts/agent-workspace.md)
- [DSSuperAgent 适配契约](contracts/dssuperagent-adapter.md)
- [DSH 执行契约](contracts/dsh-execution.md)
- [实施步骤](docs/02-implementation-plan.md)
- [原始需求归档](source/README.md)

