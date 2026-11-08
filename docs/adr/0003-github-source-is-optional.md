# ADR 0003 GitHub Source 是可选适配器

状态：已接受
日期：2026-11-08

Company Agent 的核心工作流不依赖 GitHub。事件、判断、工作项、Workspace 执行、结果和发布可以通过通用 Event 与 Channel Adapter 在本地完成验证。GitHub repository 和 Agent account 只在验收 DSSuperAgent 自带的 GitHub Source 适配器时使用，不作为核心开发、模型调用或 DSH 工作环境的前置条件。
