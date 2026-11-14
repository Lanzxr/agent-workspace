# 技术决策记录

## D001 项目采用四层定义

状态：已接受  
日期：2026-11-08

通用助理平台负责控制面。DSSuperAgent 是静默观察和执行编排的主要地基。DSH 是内部执行层。Agent Workspace 是补齐上游缺失工作环境的独立层。

## D002 不接入 Codex

状态：已接受  
日期：2026-11-08

整个项目不接入 Codex，不保留第二套专业编程执行器，也不设计依赖 Codex 的降级路径。

## D003 业务领域只能通过扩展点接入

状态：已接受  
日期：2026-11-08

领域数据、工具、术语和策略通过注册接口接入。核心编排器不包含某个业务的专用实体或分支。业务例子只用于契约和验收夹具。

## D004 不复刻上游内部实现

状态：已接受  
日期：2026-11-08

通过 DSSuperAgent Adapter 和 DSH Adapter 接入真实上游能力。上游接口未核验前只实现适配边界，不把猜测写成实现。

## D005 Agent Workspace 是独立产品边界

状态：已接受  
日期：2026-11-08

工作环境必须有独立身份、持久目录、依赖管理、资源限制、日志、成果和恢复能力。其实现可以在开发期使用 WSL2 或 Docker，在产品安装期由 Windows/Linux 安装器自动准备。

## D006 后端是权限和发布的最终裁决者

状态：已接受  
日期：2026-11-08

模型、DSSuperAgent 和 DSH 只能提出意图或结果。后端负责权限、审批、状态转换、敏感数据边界、发布策略和 Outbox 幂等。

## D007 开发验证和产品安装分开

状态：已接受  
日期：2026-11-08

开发者可以使用 native-dev、WSL2 和 Docker 验证核心逻辑。产品交付时通过安装包准备运行时和依赖，不能把手工安装 Docker 作为终端用户前置条件。

## D008 DSH 首期使用 Headless JSON Adapter

状态：已接受

日期：2026-11-15

DSH Adapter 调用官方 Headless JSON/NDJSON 接口。只有 DSH 报告 completed、工具调用和结果配对有效且声明的产物通过工作区校验，Execution 才能成功。执行结果先写工作区外的私有回执，再更新控制面；无回执的中断执行不自动重放。模型凭据只进入 DSH 的 Provider 环境，DSH 子进程按上游 credential-shaped environment scrub 规则过滤该变量。

## D009 DSH Shell 使用 Bubblewrap 用户命名空间

状态：已接受
日期：2026-11-15

WSL2 开发期由 Bubblewrap 限制 DSH 的一次性 Bash 文件和网络访问：根文件系统只读，`/mnt`、`/run`、WSL 用户目录和整个 Company Agent 私有数据目录在子进程中不可见，仅当前 Workspace 重新挂入并可按 `workspace-write` 写入。自定义 `--config` 若位于其它可见路径，会在 Shell 中被 `/dev/null` 覆盖；若配置位于 Workspace 内则拒绝执行。DSH 本身及模型 Provider 请求不进入 Bubblewrap。DSH 文件读取、持久 Shell、PowerShell、终端和工作目录切换工具关闭。缺少 Bubblewrap 或 Workspace 位于受保护路径时，执行在启动前失败。进程可见性、设备和资源配额仍未完整隔离；它不是容器或生产安全边界。产品安装器需在目标 Windows/Linux 运行时准备并重新验收等效能力。

