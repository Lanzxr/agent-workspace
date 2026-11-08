# Step 0 上游事实核验

日期：2026-11-08  
范围：DSSuperAgent、DSH、DSHA 及其工作环境、持久化和接入边界  
方法：第一方源码、官方文档、官方发行信息和本机只读探针

## 结论摘要

| 组件 | 当前事实 | 对本项目的结论 |
|---|---|---|
| DSSuperAgent | deepseek-ai/dsh-superagent-prototype 是 MIT 许可的研究原型，作为 DSH bundle 运行，已有 Source、Trigger、Thread、Eval、Policy、Ticket、Assignment、Executor、Channel 和 Outbox 等工作域 | 可以作为静默观察和执行编排的地基，但不能直接当成通用产品或生产级隔离层 |
| DSH | deepseek-ai/deepseek-harness 是 DeepSeek 官方开源 Agent Harness，MIT 许可，当前 npm CLI 为 @deepseek-ai/dsh 0.2.2-rc.1 | 可以作为内部执行层。接入优先走官方 Python SDK 或 profile/JSON-RPC 契约 |
| DSHA | DSH-APP/DSHA 是 Android 启动器，将 DeepSeek Harness 和 Ubuntu/proot 打进 APK | 只作为工作环境封装的参考，不纳入 Windows/Linux 核心运行时 |
| Agent Workspace | DSSuperAgent 有每个 Ticket 的目录和 prepare 命令，DSH 有显式 cwd、dsh_home、Session 持久化和 sandbox 插件，但上游目录配置本身不是完整隔离边界 | 我们仍需实现 Workspace Manager，负责身份、生命周期、资源和恢复 |

## 1. 核验对象和版本

### 1.1 DSSuperAgent

- 仓库：[deepseek-ai/dsh-superagent-prototype](https://github.com/deepseek-ai/dsh-superagent-prototype)
- 分支：main
- 核验指针：8c32a9dc5d72c033816c6a8110b37f40cf94fba4
- package.json 版本：0.1.0
- 许可证：MIT
- 官方声明：这是研究原型，不是官方 DeepSeek 产品，不保证生产可用。
- 来源：[README.md](https://raw.githubusercontent.com/deepseek-ai/dsh-superagent-prototype/main/README.md)

### 1.2 DSH

- 仓库：[deepseek-ai/deepseek-harness](https://github.com/deepseek-ai/deepseek-harness)
- 分支：master
- 核验指针：fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159
- 仓库许可证：MIT
- 状态：官方 README 标记为 developer preview，兼容性会发生破坏性变化。
- npm 查询结果：@deepseek-ai/dsh 0.2.2-rc.1，MIT，bin 为 dsh，Node 要求为 >=24.9.0。
- 来源：[README](https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/master/README.md)、[apps/cli/package.json](https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/master/apps/cli/package.json)、[npm package](https://www.npmjs.com/package/@deepseek-ai/dsh)

### 1.3 DSHA

- 仓库：[DSH-APP/DSHA](https://github.com/DSH-APP/DSHA)
- 许可证：MIT
- 定位：Android 启动器，内置 Ubuntu rootfs、DeepSeek Harness、proot/proroot 和移动端 Web UI。
- 它不是 Windows/Linux 服务端运行时，也不是 DSSuperAgent 的必需依赖。
- 来源：[DSHA README](https://github.com/DSH-APP/DSHA)

## 2. DSSuperAgent 已核验事实

### 2.1 运行定位

DSSuperAgent 面向单个 GitHub 仓库，也可选监听 Feishu。得到授权的请求会创建 Work Item/Ticket，由独立 DSH Session 和目录执行，再把结果写回原始会话。它以 DSH bundle 形式安装，不是独立模型服务。

来源：[DSSuperAgent README](https://raw.githubusercontent.com/deepseek-ai/dsh-superagent-prototype/main/README.md)

### 2.2 领域模型和输入

官方领域流转是：

~~~text
Source → Trigger → Thread → Eval → Decision
                              ├─ ignore
                              ├─ unavailable
                              └─ accept → Mandate → Ticket → Assignment
~~~

核心对象包括 Source、Trigger、Thread、Correlation、Channel、Policy、Eval、Mandate、Ticket、Assignment 和 Subscription。Trigger 是不可变事件和幂等单位。Thread 负责上下文和串行 Eval。Ticket 是已获授权的工作单位。Assignment 绑定 Ticket 与 Executor。

来源：[001 Domain model](https://raw.githubusercontent.com/deepseek-ai/dsh-superagent-prototype/main/docs/001-domain.md)

### 2.3 静默和主动工作策略

Policy 的 accept mode 有三种：

- off：推断候选不进入工作。
- observe：默认模式，记录正向评估但不接受工作。
- auto：正向评估可以接受推断候选。

明确请求在三种模式下都可以工作。官方建议先用 observe 观察，再切换到 auto。这与本项目的静默分析和发布分离目标一致。

来源：[000 Philosophy](https://raw.githubusercontent.com/deepseek-ai/dsh-superagent-prototype/main/docs/000-philosophy.md)、[002 Authorization](https://raw.githubusercontent.com/deepseek-ai/dsh-superagent-prototype/main/docs/002-authorization.md)

### 2.4 启动和配置

官方部署流程是：

~~~sh
npm install -g @deepseek-ai/dsh@0.2.1-rc.3
git clone <dsh-superagent-repository> dsh-superagent
cd dsh-superagent
pnpm install
pnpm build
dsh superagent --from-default-profile web --dump-config > /dev/null
dsh plugin --profile superagent add "link:$PWD"
scripts/dsh-superagent start
~~~

配置通过 $DSH_HOME/profiles/superagent/cordis.patch.yml 提供。示例设置 Web 端口 3082，并将 dsh-superagent 的 stateDir 指向绝对路径。

来源：[006 Deployment](https://raw.githubusercontent.com/deepseek-ai/dsh-superagent-prototype/main/docs/006-deployment.md)、[示例 profile patch](https://raw.githubusercontent.com/deepseek-ai/dsh-superagent-prototype/main/examples/profile.cordis.patch.yml)、[服务脚本](https://raw.githubusercontent.com/deepseek-ai/dsh-superagent-prototype/main/scripts/dsh-superagent)

### 2.5 执行环境和 Workspace

每个 Ticket 有一个 Executor Session 和一个目录，默认在 <stateDir>/work 下。首次 Assignment 前，如果目录不存在，会执行一次 prepare 命令。prepare 与 Executor 共用 DSH_SUPERAGENT_DIR、DSH_SUPERAGENT_NAME、DSH_SUPERAGENT_TICKET 等变量。

官方文档明确说明 worker preset 以操作系统用户身份运行，默认是 danger-full-access。命令包装器和目录默认值不是安全边界，绝对路径仍可能越界。官方建议为 Agent 使用专用 OS 账号。

来源：[003 Execution](https://raw.githubusercontent.com/deepseek-ai/dsh-superagent-prototype/main/docs/003-execution.md)、[006 Deployment](https://raw.githubusercontent.com/deepseek-ai/dsh-superagent-prototype/main/docs/006-deployment.md)

### 2.6 持久化和管理接口

stateDir 保存数据库、锁、命令 shim、Git 配置和默认 Ticket 目录。源码 Store 使用 SQLite，支持 schema migration、事务、版本化设置、Outbox receipts、Trigger、Thread、Decision、Ticket 和 Assignment 等持久化。

管理 API 位于 http://127.0.0.1:<port>/orchestrator/api/，要求 x-orchestrator-request: 1，并限制来源为本机地址。

来源：[Store 源码](https://github.com/deepseek-ai/dsh-superagent-prototype/blob/main/src/app/store.ts)、[007 Architecture](https://raw.githubusercontent.com/deepseek-ai/dsh-superagent-prototype/main/docs/007-architecture.md)、[006 Deployment](https://raw.githubusercontent.com/deepseek-ai/dsh-superagent-prototype/main/docs/006-deployment.md)

### 2.7 与通用项目的差距

已验证范围仍然是“单个 GitHub 仓库、可选 Feishu、以代码工作为主要对象”。以下能力不能直接假定已经具备：

- 企业微信、Telegram、QQ、WhatsApp 等渠道。
- 通用业务 Data Source 和 Tool Capability 注册。
- 多企业工作区或多 Agent 生命周期。
- 安装包自动准备 Windows/Linux 运行依赖。
- 内核级 Workspace 隔离。

## 3. DSH 已核验事实

### 3.1 启动入口

官方支持：

~~~sh
npx @deepseek-ai/dsh web
~~~

默认 Web 地址是 http://127.0.0.1:3080。CLI 还定义 web、headless、sdk、sdk-minimal 和 acp profile。调用目录是默认 workspace root，新 Web UI 首次使用时必须显式选择 workspace。

来源：[DeepSeek Harness README](https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/master/README.md)、[CLI README](https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/master/apps/cli/README.md)、[Web UI guide](https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/master/docs/user/guide/index.md)

### 3.2 Python SDK 和输入输出

官方 Python SDK 通过 newline-delimited JSON-RPC over stdio 驱动 bundled dsh CLI。DeepSeekHarness 接收 dsh_home、cwd、provider、model 和 profile，Session.run 返回 RunResult，包含 session_id、final_response、finish_reason、events 和 notifications。resume_session 可以恢复持久化历史。

官方 SDK 文档列出 Windows x64、Linux x64 和 Linux arm64 等支持目标。每次运行必须显式提供隔离的 dsh_home 或 DSH_HOME，SDK 不会隐式使用 ~/.dsh。

来源：[Python SDK README](https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/master/python/sdk/README.md)、[Python SDK guide](https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/master/docs/user/guide/python-sdk.md)、[minimal.py](https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/master/python/sdk/examples/minimal.py)

### 3.3 Workspace、文件和权限

sdk-minimal 提供平台对应的持久 Shell、read_image 和 working_directory。官方提醒持久 PTY 可以修改运行时可见的任意路径。

dsh-fs-local 的 cwd 只是相对路径解析默认值，绝对路径和 .. 可以越界。dsh-fs-sandbox 的 workspace-write 模式才会把文件变更限制到 Workspace、额外可写目录和临时目录。文件 fence 仍是可信进程中的策略检查，不是内核级隔离。Shell 需要独立的运行时隔离策略。

来源：[fs-local](https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/master/packages/fs/fs-local/README.md)、[fs-sandbox](https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/master/packages/fs/fs-sandbox/README.md)、[Python SDK examples](https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/master/python/sdk/examples/README.md)

### 3.4 Session 持久化

官方第一方 dsh-session-persistence-jsonl 将每个 Session 存为 append-only JSONL 逻辑日志，默认可以使用 Zstandard frame，也可以保存纯文本 JSONL。sdk-minimal 将未压缩日志保存到 $DSH_HOME/sessions。恢复可以修复中断的最后一轮，但不会恢复 Shell、浏览器或其他外部进程。

来源：[Session persistence JSONL](https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/master/packages/session/session-persistence-jsonl/README.md)、[Python SDK README](https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/master/python/sdk/README.md)

### 3.5 模型和凭据

官方支持 DeepSeek API key、自定义 OpenAI Chat Completions、OpenAI Responses 和 Anthropic Messages 协议。Python SDK 可以用 provider、model、base_url 和 api_key 覆盖环境变量。凭据写入 $DSH_HOME/.credentials.yaml，设置文件只保存凭据引用。

来源：[Providers guide](https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/master/docs/user/guide/providers.md)、[Python SDK README](https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/master/python/sdk/README.md)

## 4. 本机探针记录

### 4.1 Git refs

~~~text
git ls-remote https://github.com/deepseek-ai/dsh-superagent-prototype.git refs/heads/main
8c32a9dc5d72c033816c6a8110b37f40cf94fba4  refs/heads/main

git ls-remote https://github.com/deepseek-ai/deepseek-harness.git refs/heads/master
fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159  refs/heads/master
~~~

### 4.2 npm 包元数据

npm view @deepseek-ai/dsh name version license repository engines bin --json 成功，结果为 0.2.2-rc.1、MIT、dsh -> lib/bin.js 和 Node >=24.9.0。

npm pack @deepseek-ai/dsh@0.2.2-rc.1 --dry-run --json 成功，包中包含 lib/bin.js、lib/profile-boot.js、类型声明和 package.json，未包含 bundled dependencies。

### 4.3 Windows DSH CLI 启动探针

当前 Windows 有 Node.js 22.23.1 和通过 nvm 安装的 Node.js 24.15.0。官方 `@deepseek-ai/dsh@0.2.2-rc.1` 要求 Node `>=24.9.0`，因此使用 Node 24.15.0 运行：

~~~text
@deepseek-ai/dsh@0.2.2-rc.1 --version       0.2.2-rc.1
dsh --help                                   exit 0
dsh --profile headless --help                exit 0
dsh --profile sdk --help                     exit 0
dsh --profile acp --help                     exit 0
~~~

使用隔离 `DSH_HOME` 运行 `headless --json`，在没有凭据时得到真实的 `session`、`status`、`turn_end` 和 `final` NDJSON 事件，并以 `MISSING_CREDENTIAL` 退出。隔离目录生成了 profile 配置、Session JSONL/Zstd 日志和投影缓存。

这证明官方 DSH CLI 在满足 Node 版本时可以启动、接受任务输入、产生机器可读结果并持久化 Session。它没有证明模型调用已经成功。

### 4.4 DSH 真实模型端点探针

使用项目外部配置源中的模型 URL 和 API key，未输出密钥内容，在隔离 Workspace 和 `DSH_HOME` 中运行两次真实 DSH headless 请求。两次都完成了 DSH 启动、Session 建立和模型请求阶段，但上游返回：

~~~text
code: SERVER
status: 503
message: No available upstream channel. Please try again later.
~~~

当前结论是 DSH 到配置端点的调用链已经打通到上游响应，模型服务本身返回 503。不能把这次结果标记成 DSH 模型调用通过，也不能把它归因于 DSH 代码错误。

### 4.5 Python SDK 发行探针

- PyPI JSON 显示 deepseek-harness-sdk 当前版本为 0.2.1rc2。
- py -3 -m pip install --dry-run --no-deps deepseek-harness-sdk==0.2.1rc2 成功解析 Windows 通用 wheel。
- py -3 -m pip index versions deepseek-harness-sdk 在当前 pip 版本返回无匹配，这是索引命令结果与 PyPI JSON 不一致，不能据此否定 wheel。尚未安装并运行真实 SDK。

来源：[PyPI SDK metadata](https://pypi.org/pypi/deepseek-harness-sdk/json)、[PyPI runtime metadata](https://pypi.org/pypi/deepseek-harness-runtime-bin/json)

### 4.6 DSSuperAgent 构建和 Windows bundle 启动探针

在固定提交 `8c32a9dc5d72c033816c6a8110b37f40cf94fba4`、Node 24.15.0 和 pnpm 11.7.0 下：

- `pnpm install --frozen-lockfile` 成功。
- `pnpm typecheck` 成功。
- `pnpm build` 成功。
- `pnpm test` 运行了 1197 个测试，1154 通过，43 失败。

失败集中在 Windows 原生环境没有 `sh`、`sleep`、`gh`、`lark-cli`，符号链接权限和 POSIX 文件权限断言。没有出现 TypeScript 编译或打包失败。

将构建后的 `dsh-superagent` 以 link bundle 加入 DSH `superagent` profile 后，官方 DSH Web 在 `127.0.0.1:3082` 启动，但 DSSuperAgent 配置被拒绝：

~~~text
$.stateDir expect string to match regexp /^\\//
~~~

当前源码的 `stateDir` 配置约束要求 POSIX 绝对路径。Windows 原生路径 `D:/...` 不能直接使用。这是 Windows 原生运行的已验证阻塞，WSL/Linux 运行仍需继续核验。

## 5. 兼容性和阻塞项

### 已确认

1. DSSuperAgent 的静默/规则模式、事件到 Ticket 的模型、每 Ticket DSH Session 和目录，以及本地 SQLite 状态库已经有源码证据。
2. DSH 有官方 CLI、Python SDK、JSON-RPC stdio、显式 Workspace/Home、Session JSONL 持久化和基础文件 sandbox 能力。
3. 上游工作目录和文件 fence 不能独自代表产品级隔离。Workspace Manager 仍是本项目必须补齐的边界。
4. DSSuperAgent 在 Windows 原生 DSH profile 中被实际加载，但 `D:/...` 的 `stateDir` 被 POSIX 路径校验拒绝。

### 未确认

1. DSSuperAgent 0.1.0 的 engines.dsh 要求是 ^0.2.1-alpha.2，官方 DSH 当前 npm latest 是 0.2.2-rc.1。是否兼容必须在固定版本的真实安装中验证，不能仅凭版本字符串判断。
2. DSSuperAgent 当前只直接提供 GitHub 和可选 Feishu Source，其他企业通讯渠道需要新的 Source/Channel 适配。
3. DSSuperAgent 的 worker preset、DSH sandbox 和我们的 Workspace Manager 如何组合，尚未在 WSL/Linux 上做真实重启、崩溃恢复和越界测试。
4. 官方 Python SDK 当前文档指向已发布 0.2.1rc2，Windows 实际启动、模型请求和 Session 恢复尚未完成真实验收。
5. DSH 真实模型请求到达了配置端点，但上游两次返回 503，模型成功响应和 Agent 工具执行尚未验收。
6. DSSuperAgent 的 Windows 原生 `stateDir` 约束要求 POSIX 绝对路径，WSL/Linux 运行仍需继续验证。

### 下一步最小核验

1. 在 WSL2/Linux 临时目录固定一组版本：@deepseek-ai/dsh、DSSuperAgent commit 和 Node/pnpm。
2. 为 DSH 准备独立 DSH_HOME、独立 Workspace 和非生产模型凭据。
3. 先运行 DSH sdk-minimal 的一次任务，记录 RunResult、Session 文件和 Workspace 文件变化。
4. 再安装 DSSuperAgent bundle，使用 observe 模式和本地或只读 Source，记录 Trigger、Decision、Ticket、Assignment、SQLite 和 Worker 目录。
5. 通过重启、失败和越界测试后，才能把 Step 0 标记为通过并进入适配器实现。

