# 上游事实核验表

本文件只记录实际检查过的上游事实。详细证据见 [Step 0 上游事实核验报告](step0-upstream-research-agent.md)。未核验项目保持 `unknown`，不能因为名称或示例代码看起来相似就标记为可用。

## DSSuperAgent

| 项目 | 状态 |
|---|---|
| 真实源码仓库 | `deepseek-ai/dsh-superagent-prototype`，提交 `8c32a9d` |
| 启动方式 | 作为 DSH bundle 安装到 profile，运行 `dsh --profile superagent` |
| 静默观察入口 | Source → Trigger → Thread → Eval，Policy 支持 `off/observe/auto` |
| 工作计划输出 | Decision → Mandate → Ticket → Assignment，由 DSH Session 执行 |
| 执行意图输出 | Assignment 绑定 Ticket、Executor Session 和工作目录 |
| 会话恢复方式 | DSH Session 恢复，DSSuperAgent 自己恢复 Assignment |
| 持久化方式 | `stateDir` 下 SQLite、锁、Outbox 和工作目录 |
| 许可证 | MIT，官方声明为研究原型，不是官方 DeepSeek 产品 |

## DSH

| 项目 | 状态 |
|---|---|
| 真实源码或发行包 | `deepseek-ai/deepseek-harness`，提交 `fcc0ad4`，npm `@deepseek-ai/dsh@0.2.2-rc.1` |
| 会话执行入口 | `headless`、`sdk`、`sdk-minimal`、`acp` profile |
| 工具调用入口 | DSH plugin capability，包含 shell、filesystem、subagent、web 等 |
| Workspace 注入方式 | profile/SDK 显式传入 `cwd`，Session 记录工作目录 |
| 结果回传格式 | headless NDJSON 或 SDK/ACP JSON-RPC，含 Session、状态、Final/RunResult |
| 依赖和版本 | Node `>=24.9.0`，官方 README 标记 developer preview |
| 许可证 | MIT |

## 核验规则

1. 先记录仓库、版本、许可证和依赖。
2. 再用最小真实调用验证输入、输出、错误和恢复。
3. 真实能力映射到对应契约。
4. 无法验证的能力记录为阻塞，不使用同名 HTTP 接口或规则模拟冒充。

## 当前状态

- DSH CLI 在 Node 24.15.0 的 Windows 环境启动和 profile 帮助探针通过。
- DSH 隔离 `DSH_HOME`、Session 持久化和无凭据错误事件探针通过。
- DSH 真实模型请求到达配置端点，但两次返回上游 503，模型成功调用仍为阻塞。
- DSSuperAgent 类型检查和构建通过，Windows 原生测试受 POSIX 工具和符号链接条件影响。
- DSSuperAgent bundle 在 Windows profile 中被加载，但 `stateDir` 要求 POSIX 绝对路径，原生 Windows 启动被阻塞。
- Step 0 尚未通过。下一步是在 WSL/Linux 固定版本并验证 DSH、DSSuperAgent、Workspace、重启和越界行为。

