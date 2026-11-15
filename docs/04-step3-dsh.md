# Step 3：真实接入 DSH

## 本步骤交付

状态：**已完成。** Ubuntu 24.04 WSL2 使用 DSH `0.2.2-rc.1` 和已有企业模型网关完成真实任务与 Bubblewrap 文件边界验收。Company Agent 通过 DSH 官方 `headless` 配置文件读取已有的模型 URL、Key 和模型名，并执行控制面已批准的 `dsh.task` Execution Request。运行流程如下：

```text
Execution Request
  -> 验证工作状态、能力、审批、参数和预期产物
  -> 独占 Workspace 执行租约
  -> DSH Headless / JSON NDJSON
  -> 校验 Session、工具调用/结果、final.completed 和产物哈希
  -> 工作区外原子写入执行回执
  -> 写回 Execution Result / Plan / Work Item
```

DSH 不负责更改 Company Agent 的任务状态、审批或群聊发布。执行回执保存于 Company Agent 私有运行目录，工具 trace、脚本、普通日志和成果保存于对应 Workspace。`thinking`/reasoning 不作为输出或证据保存。

DSH 的一次性 Bash 工具由 Bubblewrap runner 包围：Linux 根目录只读，`/mnt`、`/run` 和整个 WSL 用户目录在 Shell 子进程中隐藏，再将当前 Agent Workspace 重新挂回原路径。自定义 `--config` 文件若不在上述隐藏目录，会被覆盖为 `/dev/null`。Shell 使用独立网络命名空间；DSH 主进程不在此命名空间内，模型 API 调用仍正常。DSH 文件工具、持久 Bash、PowerShell、终端和工作目录切换工具关闭。Bubblewrap 缺失或 Workspace 位于受保护目录时，Company Agent 在启动 Execution 前拒绝任务。

## 配置

Windows 配置源是仓库之外的：

```text
D:\work\AI工作文件夹\内部agent\company-agent-runtime\runtime.env
```

使用现有的 `AGENT_MODEL_URL`、`AGENT_MODEL_API_KEY` 和 `AGENT_MODEL_NAME`。现有 `DEEPSEEK_BASE_URL` 和 `DEEPSEEK_API_KEY` 是兼容回退项，不要求重复填写 Key。配置示例：

```dotenv
AGENT_MODEL_URL=https://model-gateway.example/v1
AGENT_MODEL_API_KEY=由部署方配置
AGENT_MODEL_NAME=deepseek-v4.1-flash
```

在 PowerShell 中同步到 `Ubuntu-24.04`：

```powershell
cd 'D:\work\AI工作文件夹\内部agent\company-agent'
.\scripts\sync-runtime-config.ps1
```

同步位置是 WSL 用户目录 `~/.config/company-agent/runtime.env`，目录权限为 `0700`、配置文件为 `0600`。Linux 上的 Company Agent 默认从这里读取配置。DSH 的版本固定为当前已经核验并安装的 `0.2.2-rc.1`；适配器查找已安装的 `dsh` 命令或用户 NPM 缓存里的精确版本，不在每次任务启动时下载或执行 `npx`。

Bubblewrap 需要在 WSL 中可执行。适配器先查找 PATH 中的 `bwrap`，否则查找用户安装路径 `~/.local/opt/bubblewrap/usr/bin/bwrap`；找不到时执行失败，不会退回到未隔离 Shell。Agent Workspace 必须位于 WSL Linux 文件系统中，不能位于 `/mnt`、`/run`、`/tmp`、整个用户目录或包含用户配置/Company Agent 私有运行数据的目录中。默认的 `~/.local/share/company-agent/workspaces/<workspace_id>` 布局符合要求。适配器隐藏整个 `--data-dir`，再把当前 Workspace 挂回去，因此自定义数据目录可以位于用户目录之外。

## 本地开发使用

以下命令从仓库根目录在 WSL 运行。`COMPANY_AGENT_DATA` 是产品运行数据目录，和 Git checkout、Agent Workspace 分开：

```bash
cd /mnt/d/work/AI工作文件夹/内部agent/company-agent
export PYTHONPATH="$PWD"
export COMPANY_AGENT_DATA="$HOME/.local/share/company-agent"
python3 -m app --data-dir "$COMPANY_AGENT_DATA" workspace operations
```

把任务正文保存到本机的 `task.md`。提交一个低风险本地开发任务并指定必须生成的产物：

```bash
python3 -m app --data-dir "$COMPANY_AGENT_DATA" submit \
  --workspace operations \
  --task-file ./task.md \
  --artifact report.json
```

命令会输出 `execution_id`，再显式执行并读取持久结果：

```bash
python3 -m app --data-dir "$COMPANY_AGENT_DATA" execute <execution_id>
python3 -m app --data-dir "$COMPANY_AGENT_DATA" result <execution_id>
```

`execute` 的配置文件默认是 `~/.config/company-agent/runtime.env`，也可使用 `--config` 指定。结果包含任务状态、工具 trace 引用、产物相对路径和 SHA-256。`result` 只读保存结果，不会再调用模型。`submit` 是本地开发入口，用来建立可审计的低风险 Work Item/Plan/Execution Request；真实产品中的自动判断和任务接受将在后续 DSSuperAgent Adapter 与策略引擎中完成。

## 真实验收

在 Windows PowerShell 中先同步配置，再从仓库根目录运行：

```powershell
wsl.exe -d Ubuntu-24.04 -- bash -lc 'cd /mnt/d/work/AI工作文件夹/内部agent/company-agent && PYTHONPATH=$PWD bash scripts/verify-dsh-wsl.sh'
```

验收先由本地控制面在 Workspace 写入只读验收脚本并记录 SHA-256；真实 DSH Bash 必须执行它，分别尝试读取真实 WSL `runtime.env`、Windows 挂载中的原始 `runtime.env` 和私有数据目录中的 `control.sqlite`，并检查 Docker socket、网络命名空间和外网连接。脚本会生成访问结果并在任一边界未生效时失败。独立进程验证探测脚本未被改动、访问结果、DSH trace 中的 Bash 调用与成功标记、业务 JSON、文件 SHA-256、脚本可重复运行、模型密钥未进入工具进程，并重试同一 execution 确认没有重复执行。

验收结果：4 行输入合计 42，A 类 17、B 类 25。真实任务只调用 `bash`；`config_file_hidden`、`windows_mount_hidden`、`private_data_hidden`、`docker_socket_hidden`、`network_namespace_isolated`、`network_egress_blocked` 均为 `true`，`model_key_visible` 为 `false`。这七项结果确认 Bash 无法读取 WSL 配置、Windows 源配置、Company Agent 私有数据库或 Docker socket，也不能直接出网，且看不到模型 Key；DSH 主进程仍成功调用模型 Provider。独立进程输出 `REAL_DSH_EXECUTION_AND_RESTART_OK` 与 `BUBBLEWRAP_SHELL_ACCESS_BOUNDARY_OK`，确认验收探测脚本未被修改、业务结果和产物哈希正确、执行结果持久化，以及重复派发没有重跑工具。

本次复验的 Company Agent 数据根目录在用户目录外：`/var/tmp/company-agent-acceptance-step3-20261115-rerun`。DSH 版本为 `0.2.2-rc.1`，运行时为 Ubuntu 24.04 / Python 3.12.3。独立复查输出 `REAL_DSH_EXECUTION_AND_RESTART_OK`、`BUBBLEWRAP_SHELL_ACCESS_BOUNDARY_OK`；七项探测结果保存在该目录对应 Workspace 的 `artifacts/sandbox-access.json`，执行 trace 位于 `workspaces/acceptance-agent/logs/dsh/acceptance-1.events.jsonl`。

模拟 DSH 只覆盖自动化协议和失败路径测试，不能代替上述真实模型/DSH 验收。

## 当前边界

- DSH `workspace-write` 约束文件写入；Bubblewrap 额外限制一次性 Bash 的可见目录。DSH 本身和模型请求不在 Bubblewrap 内。
- `/mnt`、`/run` 和 WSL 用户目录对 Bash 子进程隐藏；当前 Workspace 重新挂入。Linux 根目录中的其它只读位置仍可见，进程可见性、设备和资源配额也未完整隔离。
- 这不是 Docker/容器/虚拟机隔离，也不能处理生产密钥或执行生产操作。正式安装包仍需为各平台准备并验收隔离 runner。
- DSH 内置 Web、文件工具、替代 Shell 和终端工具关闭；一次性 Bash 不能出网，模型 Provider 调用由隔离外的 DSH 主进程完成。
- 超时、协议异常、DSH 不完整结束或缺少预期产物都会失败。
- 进程崩溃且没有持久回执时禁止自动重放。先检查副作用，再创建新的执行请求。
- 本步骤不包含完整资源配额、外发发布链路、DSSuperAgent 自主观察决策或 Windows/Linux 安装包。
