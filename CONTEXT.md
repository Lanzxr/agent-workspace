# Company Agent Context

这个上下文定义通用助理平台的领域语言。它区分控制面、上游 Agent 地基、执行层、工作环境、外部工作来源和模型连接，避免把一次具体渠道验收误认为核心架构。

## 核心架构

**Company Agent Control Plane**：保存通用工作状态、权限、审批、记忆、审计和发布决定的产品边界。
_Avoid_: 业务机器人、聊天窗口

**DSSuperAgent**：负责静默观察、工作判断、计划组织和执行编排的上游 Agent 地基。
_Avoid_: 通用控制面、模型接口

**DSH**：负责 Agent 会话、工具调用和结果回传的内部执行层。
_Avoid_: 业务状态源、通讯渠道

**Agent Workspace**：属于 Agent 的可持久工作空间，保存运行所需的文件、工具、成果和环境状态。
_Avoid_: 临时目录、宿主机根目录

## 外部工作来源

**Source Scope**：一个外部工作来源及其允许观察和行动的资源范围。
_Avoid_: 数据库、租户

**Agent Identity**：Agent 在某个外部来源上读取事件、提交操作或发布结果时使用的外部身份。
_Avoid_: 当前操作者、模型身份

**Authorization Policy**：规定哪些身份可以发起工作、哪些工作可以自动接受、哪些执行和发布需要审批的规则。
_Avoid_: 提示词、个人偏好

**Channel Adapter**：把外部来源转换为标准 Event，并把 Outbox 转换为外部消息的边界。
_Avoid_: 任务状态源

## 模型连接

**Model Endpoint**：提供模型请求和响应的外部服务地址及其协议。
_Avoid_: Agent Runtime、模型名称

**Provider Route**：一个可被 Agent 选择的模型连接配置，包含外部地址、协议、凭据引用和模型目录。
_Avoid_: API Key、单次请求

**Custom Model Provider**：由项目或部署方定义的 Provider Route，用于连接内置目录之外的网关或自托管服务。
_Avoid_: 直接 HTTP 适配器

## 工作对象

**Event**：来自来源、调度或系统的一次不可变工作事件。
_Avoid_: 指令、回复

**Work Item**：经过控制面跟踪的通用工作事项，拥有目标、状态、风险、完成条件和来源事件。
_Avoid_: 业务订单、聊天消息

**Memory**：带来源和可信度的长期事实、规则或工作经验。
_Avoid_: 临时上下文、模型缓存

**Publication Decision**：控制面决定某个结果是否发送给指定 Conversation 的记录。
_Avoid_: 自动回复
