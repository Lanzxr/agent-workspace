# Step 4：接入 DSSuperAgent

## 目标

把标准化事件送入 DSSuperAgent 的真实 `Source → Trigger → Thread → Eval → Decision` 流程，取得结构化助理意图，由 Company Agent 保存工作状态并执行后端权限检查；获准的工作通过 DSH 在独立 Agent Workspace 中执行，执行结果再回传同一 DSSuperAgent 会话。

```text
标准事件和有限会话上下文
  → DSSuperAgent Source / Trigger / Thread / Eval / Decision
  → observe 或 propose_work 结构化意图
  → Company Agent AssistantRun / Work Item / Plan
  → 后端策略或审批
  → DSH 在 Agent Workspace 中执行
  → 持久化结果并回传 DSSuperAgent
  → 保存发布建议，留给 Step 5 决定是否投递
```

## 职责与权限

- DSSuperAgent 负责静默判断和提出工作意图，不直接创建 Ticket、执行工作或发送消息。
- Company Agent 负责持久化工作状态、检查能力和权限、调用 DSH、保存执行结果及发布建议。
- 事件中的能力清单只说明本次事件允许讨论哪些执行能力，不构成授权。模型提出的能力必须同时存在于事件和后端配置中。
- 默认策略不自动执行。开发验收可通过命令行显式允许 `dsh.task`；其他工作走已有审批流程，DSH Dispatcher 仍会再次校验。
- Step 4 只保存 `suppress`、`private_nudge`、`group_update` 或 `urgent_escalation` 建议，不创建 Outbox，不向任何群或个人发送消息。
- 当前 DSSuperAgent HTTP 地址必须是本机 loopback 地址，不传递 GitHub 凭据或仓库身份。

## DSSuperAgent 桥接补丁

上游需要 Company Agent 专用 Source、事件接收 API、`assistant_intent` 结构化输出和受控联网工具。项目保存了可复现补丁：[Step 4 基础桥接](../patches/dssuperagent-company-agent-step4.patch)、[资料库检索扩展](../patches/dssuperagent-knowledge-bridge.patch) 和 [受控联网搜索](../patches/dssuperagent-controlled-web-search.patch)。后两个补丁须按顺序在基础桥接补丁之后应用，联网补丁最后应用。基础补丁基于 DSSuperAgent 提交 `8c32a9dc5d72c033816c6a8110b37f40cf94fba4`。

在专用的 DSSuperAgent checkout 中应用，不要复用带有个人配置、DSH_HOME 或状态库的运行目录：

```bash
git rev-parse HEAD
git apply --check /path/to/company-agent/patches/dssuperagent-company-agent-step4.patch
git apply /path/to/company-agent/patches/dssuperagent-company-agent-step4.patch
git apply --check /path/to/company-agent/patches/dssuperagent-knowledge-bridge.patch
git apply /path/to/company-agent/patches/dssuperagent-knowledge-bridge.patch
git apply --check /path/to/company-agent/patches/dssuperagent-controlled-web-search.patch
git apply /path/to/company-agent/patches/dssuperagent-controlled-web-search.patch
pnpm typecheck
pnpm exec vitest run --reporter=dot
pnpm build
```

产品发布时应把补丁固定到受控的上游版本构建流程中。当前 Step 4 不要求连接 GitHub，也不修改 DSSuperAgent 的个人 Profile 或既有状态目录。

## 本地开发验证

运行环境是 WSL2/Linux。Company Agent 的 DSH 模型配置继续使用 WSL 中的 `~/.config/company-agent/runtime.env`；DSSuperAgent 自己的模型配置仍由它的运行环境管理。事件样例位于 `tests/fixtures/company-agent-event.json`，每次新评估应使用新的 `event_id`。

只验证静默判断和工作提案，不授权执行：

```bash
cd /mnt/d/work/AI工作文件夹/内部agent/company-agent
export PYTHONPATH="$PWD"
export COMPANY_AGENT_DATA="$HOME/.local/share/company-agent"
python3 -m app --data-dir "$COMPANY_AGENT_DATA" assistant-event \
  --event-file ./tests/fixtures/company-agent-event.json \
  --dssuperagent-url http://127.0.0.1:3082
```

完整验证 DSH 与 Workspace 链路时，显式允许本地低风险能力：

```bash
python3 -m app --data-dir "$COMPANY_AGENT_DATA" assistant-event \
  --event-file ./tests/fixtures/company-agent-event.json \
  --dssuperagent-url http://127.0.0.1:3082 \
  --workspace step4-agent \
  --auto-execute-capability dsh.task
```

命令输出持久化的 AssistantRun、Decision、Work Item、Plan、Execution ID、结果引用和发布建议，不输出模型 Key。相同事件重跑会读取已保存状态，不重复执行已完成的 DSH 请求。

## 失败与恢复

- 上游连接失败、超时、JSON 或结构化意图不合约时，运行记录保存错误类别，不能将 Mock 结果当作上游验收。
- DSH 成功结果会持久化。若只是上游结果回传失败，可用相同事件重试；稳定的结果事件 ID 会让补投保持幂等，已完成的 DSH 调用不会再次运行。
- DSH 的失败、超时或中断也会持久化为终态。系统不会用同一 Execution ID 自动重放可能产生副作用的任务；需要先检查 Workspace 中的执行痕迹，再通过后续的显式恢复流程授权新请求。

## 验收证据

- Company Agent：`py -m unittest discover -v`，48 项通过，4 项因 Windows 不具备 POSIX 权限、Shell 或符号链接条件而跳过。
- 真实 DSSuperAgent + 模型 + DSH + Workspace 链路已在隔离的 WSL2 验收目录执行：一次观察事件保存为 `observed`；一次工作提案保存为 Work Item 和 Plan，三个 DSH 步骤均成功并回传同一 DSSuperAgent 会话。
- 同一工作事件重跑后仍是原 Work Item 与三个 Execution，无重复调用；本步骤没有产生 Outbox 消息。
- 验收使用专用的 DSH_HOME、DSSuperAgent 状态目录和 Company Agent 数据目录，未使用个人 Profile 或既有状态库。验收数据保留在隔离临时目录，便于复查。
- 补丁可在固定上游基线上通过 `git apply --check`。补丁工作树的 `pnpm typecheck`、`pnpm build` 和模型接口测试（7/7）通过。
- 补丁工作树完整测试为 1,199/1,200 通过；未应用补丁的干净上游基线为 1,196/1,197 通过。两边唯一失败都是 `tests/sources.spec.ts` 中已有的飞书事件消费者用例，单独复跑仍复现：测试期望格式错误状态，实际读到 `ready`。该失败不在补丁涉及文件中，因此记录为上游基线问题，不计作 Step 4 回归。
- GitHub 和 Docker 都不是本地开发验证的前置条件。

## 本步骤不包含

本步骤不连接飞书、企业微信、微信、QQ、Telegram 或 WhatsApp，不实现业务数据源抓取、长期策略管理、发布 Outbox 投递、Linux 资源配额或 Windows/Linux 安装包。这些属于后续步骤。
