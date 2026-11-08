# 架构重置记录

## 原因

此前文档把项目描述成由自建业务编排器直接驱动 DSH 的任务系统，并把具体主动分析案例写进核心流程。该描述没有准确表达 DSSuperAgent 的地基角色，也没有把 Agent Workspace 作为独立补齐层。

## 新定义

- DSSuperAgent：静默观察、工作判断、计划组织和执行编排的主要地基。
- DSH：内部执行层。
- Agent Workspace：补齐上游工作环境的独立能力层。
- Company Agent：通用控制面和产品化边界。

## 处理方式

旧实现和旧文档已保存到 Git 分支 `archive/pre-architecture-reset`，不再作为 `main` 的基线。原始需求文件保留在 `source/`，只用于追溯。新的实现必须从当前契约和事实核验开始。

