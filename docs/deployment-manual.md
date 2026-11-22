# Company Agent 部署手册（WSL2 / Linux 服务器 / 通用 Linux）

本手册面向将 `company-agent` 与 `dssuperagent-company-agent-step4` 从源码构建并部署到自有设备的运维或开发者，覆盖 **Windows + WSL2** 与 **独立 Linux 服务器 / 云主机** 两类完整场景。

---

## 一、部署前环境要求与端口规划

### 1.1 系统与运行依赖

| 组件 | 版本 / 要求 | 说明 |
|---|---|---|
| **操作系统** | Ubuntu 22.04 / 24.04 或同类现代 Linux（systemd + cgroups v2） | WSL2 需启用 `systemd=true`；内核需允许用户命名空间（供 Bubblewrap 创建隔离命名空间） |
| **Bubblewrap** | `bwrap` 可执行文件 | **强制必需**：DSH 所有 `bash` 沙箱均通过 `bwrap --unshare-net` 隔离 |
| **Python** | Python `3.11+`（含 `venv` 与 `pip`） | 运行控制面、QQ 与企业微信渠道、Tool Forge、Knowledge & Web Gate；运行依赖需 `websockets>=12,<17` 与 `cryptography>=42,<52`（可选 `pypdf`） |
| **Node.js & pnpm** | Node `^22.19.0` 或 `>=24.0.0`；`pnpm`（`11.x`） | 运行 DSSuperAgent 控制编排器与 DSH；构建 `dssuperagent-company-agent-step4` 时需要 `pnpm` |
| **DSH 运行时** | `@deepseek-ai/dsh@0.2.2-rc.1` | 须固化安装到非 `/tmp` 目录下的稳定 `node_modules` |
| **Docker + Compose** | 可选（启用联网搜索与长期资料库时必需） | 用于运行回环绑定的 `SearXNG`（默认 `:28770`）与 `AnythingLLM v1.19.0`（默认 `:28771`） |

### 1.2 本地服务与低冲突默认端口清单

> [!IMPORTANT]
> 为避开开发者高频端口（如 `3000–3100`、`8000–9000`、`8888` Jupyter 等）以及 Linux 默认临时端口区（`32768–60999`），系统全部默认采用连续的 **`28765–28771`** 低冲突端口段，且只允许绑定回环地址（`127.0.0.1`）。
> 若机器仍与局部端口碰撞，只需在 `runtime.env` 加一行 **`COMPANY_AGENT_PORT_BASE=<空闲起始端口>`**（例如 `29765`），即可一键整体平移全部 7 个服务并自动联动派生内部 URL；也可按单个服务端口变量单独覆写。安装器和各服务启动前会自动执行端口可用性预检，发生占用时立即给出精准排错提示。

| 默认端口 | 环境变量覆盖（或由 `PORT_BASE` + 偏移推导） | 所属进程 / 服务 | 作用说明 |
|---|---|---|---|
| `127.0.0.1:28765` | `COMPANY_AGENT_ADMIN_PORT`（`+0`） | `company-agent@admin.service` | Admin 控制台 Web UI 与 `/api/*`、`/api/forge/*` 管理接口 |
| `127.0.0.1:28766` | `COMPANY_AGENT_KNOWLEDGE_API_PORT`（`+1`） | `company-agent@admin.service`（或 `qq`） | AnythingLLM 授权检索内部桥 |
| `127.0.0.1:28767` | `COMPANY_AGENT_WEB_GATE_PORT`（`+2`） | `company-agent@admin.service`（或 `qq`） | DSSuperAgent / DSH 联网检索审批网闸 |
| `127.0.0.1:28768` | `COMPANY_AGENT_FORGE_MCP_PORT`（`+3`） | `company-agent@admin.service`（或 `qq`） | Tool Forge 分级 Streamable HTTP MCP 服务 |
| `127.0.0.1:28769` | `COMPANY_AGENT_DSSUPERAGENT_PORT`（`+4`） | `company-agent-superagent.service` | DSSuperAgent 官方 DSH 常驻协调引擎（启动时通过 `--patch` 动态覆写） |
| `127.0.0.1:28770` | `COMPANY_AGENT_SEARCH_PORT`（`+5`） | Docker `company-agent-search` | SearXNG 元搜索引擎（仅由 Web Gate 在回环侧调用） |
| `127.0.0.1:28771` | `COMPANY_AGENT_ANYTHINGLLM_PORT`（`+6`） | Docker `anythingllm` | 自托管长期资料库与 OCR 服务 |

---

## 二、规划部署账号与目录结构

为保证隔离性，服务**不能使用 `root` 运行**，请先准备或指定一个非特权 Linux 账号（本文用 `<USER>` 表示，例如新建专用账户 `companyagent` 或 WSL 默认非 root 用户名 `alice`）。

```text
/home/<USER>/
├── .config/company-agent/
│   ├── runtime.env                  # 核心私钥与运行配置 (权限 0600)
│   └── search.env                   # SearXNG SEARXNG_SECRET (可选, 0600)
├── .local/share/company-agent/      # 数据与运行根目录 (权限 0700, 严禁放 /tmp /mnt /run)
│   ├── venv/                        # Python 虚拟环境 (含 websockets / cryptography)
│   ├── control.sqlite               # 控制面 SQLite 数据库
│   ├── forge-secrets.json           # Tool Forge 第三方凭据保险库 (0600)
│   ├── forge-mcp-tokens.json        # Tool Forge 三层内部 MCP 令牌 (0600)
│   ├── workspaces/                  # 各群/会话隔离工作区
│   └── shared-tools/{common,internal}/
├── company-agent-deploy/            # 发布安装器管理的不可变版本目录
│   ├── releases/<release_id>/
│   ├── backups/<timestamp>/
│   └── current -> releases/<release_id>
└── company-agent-step0/
    └── superagent-home/             # DSH 运行家目录 (DSH_HOME)
```

---

## 三、初始化 Linux 运行环境（WSL2 与纯 Linux 服务器通用）

### 3.1 WSL2 专属前置（纯 Linux 服务器可跳过此步）

1. 在 Windows 宿主启用 WSL2 Ubuntu-24.04，确保 `/etc/wsl.conf` 开启了 systemd：
   ```ini
   [boot]
   systemd=true
   ```
   修改后在 PowerShell 执行 `wsl --shutdown` 再重新进入生效。
2. **重要**：Windows 下可将源码放在本地盘符（如 `D:\work\company-agent`），但**数据目录、`runtime.env`、DSH home 与 Workspace 必须留在 WSL 的 Linux ext4 目录**（例如 `/home/<USER>/...`），切勿放到 `/mnt/c`、`/mnt/d` 或 `/tmp` 下（控制面会直接拒绝 `/tmp`、`/mnt`、`/run`）。

### 3.2 Linux 内核与基础软件安装

以目标运维/sudo 账号登录 Linux（或进入 WSL）：

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip bubblewrap git curl xz-utils
```

1. **检查 Bubblewrap 与 User Namespace 可用性**（Ubuntu 24.04 必查）：
   ```bash
   bwrap --unshare-net --ro-bind / / /bin/true
   ```
   若在 Ubuntu 24.04 报 `setting up uid map: Permission denied`（AppArmor 默认限制非特权 userns），按你的服务器安全策略为 `bwrap` 配置 AppArmor profile 或放行非特权 userns 后再验证上述命令退出码为 `0`。

2. **创建非特权部署账号（如果尚未创建）**：
   ```bash
   sudo useradd -m -s /bin/bash <USER>   # 若使用现有普通用户可跳过
   ```

3. **准备 Python 虚拟环境**：
   ```bash
   sudo -u <USER> mkdir -p /home/<USER>/.local/share/company-agent /home/<USER>/.config/company-agent /home/<USER>/company-agent-step0/superagent-home
   sudo -u <USER> chmod 700 /home/<USER>/.local/share/company-agent /home/<USER>/.config/company-agent
   sudo -u <USER> python3 -m venv /home/<USER>/.local/share/company-agent/venv
   sudo -u <USER> /home/<USER>/.local/share/company-agent/venv/bin/pip install --upgrade pip "websockets>=12,<17" "cryptography>=42,<52" pypdf
   ```

4. **安装 Node.js（22.19+ 或 24+）、pnpm 与 `@deepseek-ai/dsh@0.2.2-rc.1`**：
   - 将 Node 安装到目标用户或系统稳定路径（例如 `/home/<USER>/node-v24.x-linux-x64/bin/node` 或 `/usr/local/bin/node`），并全局或独立缓存安装稳定版 DSH（切勿把安装器临时产生的 `/tmp/...` 路径填进构建参数）：
   ```bash
   # 示例：将 DSH 固定安装到用户私有 npm 前缀下
   sudo -u <USER> npm --prefix /home/<USER>/.dsh-runtime install --no-save @deepseek-ai/dsh@0.2.2-rc.1
   ```
   记录下三个绝对 Linux 路径，后续构建发布包时会用到：
   - `<NODE_BIN>`：如 `/usr/local/bin/node` 或 `/home/<USER>/node-v24.15.0-linux-x64/bin/node`
   - `<DSH_BIN>`：如 `/home/<USER>/.dsh-runtime/node_modules/@deepseek-ai/dsh/lib/bin.js`
   - `<DSH_HOME>`：`/home/<USER>/company-agent-step0/superagent-home`

---

## 四、准备私密配置文件（`runtime.env`）

从源码仓库复制模板 [runtime.env.example](file:///D:/work/AI工作文件夹/内部agent/company-agent/runtime.env.example) 到私有配置路径，切勿提交回 Git：

- **场景 A（Windows 编辑后同步到 WSL2）**：在仓库外创建私有配置文件（如 `company-agent-runtime/runtime.env`），填写好之后运行：
  ```powershell
  .\company-agent\scripts\sync-runtime-config.ps1 -Source <你的Windows配置路径>\runtime.env -Distribution Ubuntu-24.04 -WslUser <USER>
  ```
- **场景 B（纯 Linux 服务器直接编写）**：
  ```bash
  sudo -u <USER> cp <源码目录>/company-agent/runtime.env.example /home/<USER>/.config/company-agent/runtime.env
  sudo -u <USER> chmod 600 /home/<USER>/.config/company-agent/runtime.env
  ```

必填核心配置项核对清单：

```dotenv
# 1. 上游大模型与 DSH 执行（必填）
AGENT_MODEL_URL=https://api.example.com/v1
AGENT_MODEL_NAME=deepseek-chat
AGENT_MODEL_API_KEY=<替换为模型接口密钥>

# 2. 本地管理控制台鉴权（必填：强随机 32+ 字符）
COMPANY_AGENT_ADMIN_TOKEN=<替换为强随机管理员令牌>

# 3. QQ Bot 接入（仅启用 qq-serve 时必填；与企业微信互不冲突可单独或双开）
QQBOT_APP_ID=<你的QQ机器人AppID>
QQBOT_CLIENT_SECRET=<你的QQ机器人Secret>
QQBOT_ALLOWED_PUBLICATIONS=group_update
QQBOT_AUTO_EXECUTE_CAPABILITIES=dsh.task

# 3b. 企业微信（WeCom）智能机器人长连接接入（仅启用 wecom-serve 时必填）
WECOM_BOT_ID=<你的企微机器人BotID>
WECOM_BOT_SECRET=<你的企微机器人Secret>
WECOM_ALLOWED_PUBLICATIONS=group_update
WECOM_AUTO_EXECUTE_CAPABILITIES=dsh.task
COMPANY_AGENT_WECOM_WORKSPACE=wecom-files

# 4. 端口与内部网关配置（默认 28765-28771；若冲突只需改 COMPANY_AGENT_PORT_BASE 即可整体平移）
# COMPANY_AGENT_PORT_BASE=28765
COMPANY_AGENT_WEB_GATE_OWNER=admin
COMPANY_AGENT_FORGE_MCP_ENABLED=true
COMPANY_AGENT_FORGE_MCP_HOST=127.0.0.1
COMPANY_AGENT_FORGE_MCP_OWNER=admin
```

> [!TIP]
> 在使用 **Tool Forge** 对话式生成业务工具之前，请确保至少在 `runtime.env` 填写好上述 1～4 项；如有现成外部 MCP 需要静态接入，见 [导入 MCP 手册](mcp-manual.md)。

---

## 五、部署可选配套容器服务（SearXNG 与 AnythingLLM）

如不需要全网搜索和长期文档知识库，可先用 `--skip-docker` 完成核心服务安装；如需启用完整能力，请先部署容器：

### 5.1 启用 SearXNG 受控搜索网关

```bash
sudo -u <USER> bash -c 'umask 077 && printf "SEARXNG_SECRET=%s\n" "$(openssl rand -hex 32)" > /home/<USER>/.config/company-agent/search.env'
```
后续执行 `install-release.py`（不加 `--skip-docker`）会自动拉起 `company-agent-search` Compose 项目（默认监听 `127.0.0.1:28770`，并自动沿用 `runtime.env` 中设置的 `COMPANY_AGENT_PORT_BASE` / `COMPANY_AGENT_SEARCH_PORT` 或已有 `COMPANY_AGENT_WEB_SEARCH_URL` 端口）。

### 5.2 启用 AnythingLLM 长期资料库与出网防火墙

1. 复制 [anythingllm.env.example](file:///D:/work/AI工作文件夹/内部agent/company-agent/deploy/anythingllm/anythingllm.env.example) 到私有路径（如 `/home/<USER>/.config/company-agent/anythingllm.env`，权限 `0600`），填写 LLM 与 Embedding 接口信息；如需避让宿主端口可设置 `COMPANY_AGENT_ANYTHINGLLM_PORT`。
2. 启动容器（预热中文 OCR 模型 `chi_sim,eng`）：
   ```bash
   docker compose -f <源码目录>/company-agent/deploy/anythingllm/docker-compose.yml --env-file /home/<USER>/.config/company-agent/anythingllm.env up -d
   ```
3. 登录 `http://127.0.0.1:28771`（或你的 `COMPANY_AGENT_ANYTHINGLLM_PORT`）初始化工作区并申请 Developer API Key，将其填回 `runtime.env`：
   ```dotenv
   ANYTHINGLLM_BASE_URL=http://127.0.0.1:28771
   ANYTHINGLLM_API_KEY=<生成的API_Key>
   COMPANY_AGENT_KNOWLEDGE_GRANT_SECRET=<32+位随机HMAC密钥>
   ```
4. （可选）按需将 [anythingllm-egress-firewall.service](file:///D:/work/AI工作文件夹/内部agent/company-agent/deploy/anythingllm/anythingllm-egress-firewall.service) 与 [apply-egress-firewall.sh](file:///D:/work/AI工作文件夹/内部agent/company-agent/deploy/anythingllm/apply-egress-firewall.sh) 应用到宿主，仅放行必需出网网络。

---

## 六、从源码构建发布包并安装启动

项目严格采用 **源码构建不可变归档 → 原子化切换部署** 流程（可在 Windows 构包传进 WSL2，也可在 Linux 服务器本机直接构建）：

### 6.1 构建 DSSuperAgent 与 Company Agent Wheel

在源码仓库根目录执行：

```bash
# 1. 构建 DSSuperAgent 适配器
cd dssuperagent-company-agent-step4
pnpm install --frozen-lockfile
pnpm build
cd ..

# 2. 构建 Company Agent Wheel
mkdir -p /tmp/ca-wheels
python3 -m pip wheel --no-deps --no-build-isolation -w /tmp/ca-wheels ./company-agent

# 3. 打包完整发布归档 (.tar.gz)
python3 company-agent/scripts/build-release.py \
  --dss-source ./dssuperagent-company-agent-step4 \
  --wheel /tmp/ca-wheels/company_agent_control_plane-0.1.0-py3-none-any.whl \
  --node <NODE_BIN> \
  --dsh-bin <DSH_BIN> \
  --dsh-home <DSH_HOME>
```
构建成功会输出类似：`build/releases/<RELEASE_ID>.tar.gz`（Windows 下若在 WSL 安装，可直接通过 `/mnt/d/.../company-agent/build/releases/<RELEASE_ID>.tar.gz` 引用）。

### 6.2 使用可配置部署用户安装与启动（root 执行）

[install-release.py](file:///D:/work/AI工作文件夹/内部agent/company-agent/scripts/install-release.py) 支持通过 `--user`（或环境变量 `COMPANY_AGENT_DEPLOY_USER` / `SUDO_USER`）将 systemd 单元模板与目录归属渲染到指定普通账号，且安装前自动完成配置校验、备份 `control.sqlite` / `runtime.env` 并校验归档哈希：

```bash
# 完整部署（含 SearXNG & egress firewall）
sudo python3 ./company-agent/scripts/install-release.py \
  ./company-agent/build/releases/<RELEASE_ID>.tar.gz \
  --user <USER>

# 如暂未启用 Docker SearXNG / AnythingLLM，添加 --skip-docker：
sudo python3 ./company-agent/scripts/install-release.py \
  ./company-agent/build/releases/<RELEASE_ID>.tar.gz \
  --user <USER> \
  --skip-docker
```

可选高级覆盖参数：`--group`、`--home`、`--deploy-dir`、`--data-dir`、`--config`（默认自动按目标账号家目录推导，不能置于 `/tmp`、`/mnt` 或 `/run`）。

### 6.3 首次配置 DSSuperAgent 上游面板

服务拉起后，查看 `company-agent-superagent.service` 日志中打印的带一次性 token 的本机地址（默认 `http://127.0.0.1:28769/?token=...`）：

- **WSL2 本机**：直接在 Windows 浏览器打开即可；
- **纯 Linux 云/内网服务器**：通过 SSH 隧道映射到本地浏览：
  ```bash
  ssh -N -L 28765:127.0.0.1:28765 -L 28769:127.0.0.1:28769 <SSH用户>@<服务器IP>
  ```
  然后在本地浏览器打开 `http://127.0.0.1:28769/?token=...` 确认模型 Provider 与插件状态就绪，再打开 `http://127.0.0.1:28765` 登录管理控制台（如调整了端口或 `COMPANY_AGENT_PORT_BASE` 则替换为对应实际端口）。

---

## 七、上线巡检、回滚与常见排障

### 7.1 服务与端口自检清单

```bash
# 1. 核心单元应处于 active（按实际配置的渠道追加 company-agent-qq.service / company-agent-wecom.service）
systemctl is-active company-agent.target company-agent@admin.service company-agent-superagent.service
systemctl is-active company-agent-qq.service company-agent-wecom.service   # 仅检测实际已配置凭据启用的渠道

# 2. 回环监听端口确认（默认 28765-28771；旧环境或自定义端口按 runtime.env 实际端口查）
ss -ltnp | grep -E '2876[5-9]|876[5-8]|3082'

# 3. Tool Forge MCP 健康与权限确认（替换为实际 FORGE_MCP_PORT）
curl -fsS http://127.0.0.1:28768/health
stat -c '%a %U' /home/<USER>/.local/share/company-agent/forge-mcp-tokens.json   # 应为 600 <USER>
```

### 7.2 升级与快速回滚

- **平滑升级**：按第六步重新构包并执行 `install-release.py`，`control.sqlite`、`forge-secrets.json`、配置与工作区全量保留，升级前快照保存在 `/home/<USER>/company-agent-deploy/backups/<UTC时间戳>/`。
- **回滚上一版本**：
  ```bash
  sudo systemctl stop company-agent.target
  sudo ln -sfn /home/<USER>/company-agent-deploy/releases/<旧RELEASE_ID> /home/<USER>/company-agent-deploy/current
  sudo systemctl start company-agent.target
  ```
  如需连带恢复数据库，再将对应备份目录的 `company-agent.sqlite` 拷回 `~/.local/share/company-agent/control.sqlite` 后启动。

### 7.3 常见启动故障排查

1. **`Agent Workspace must be outside /mnt, /run and /tmp`**：确认 `--data-dir` 与 `COMPANY_AGENT_ACCEPTANCE_ROOT` 未落在 `/mnt`、`/run` 或 `/tmp`；WSL 跑单元测试时可设置 `TMPDIR=/var/tmp/...`。
2. **`Bubblewrap is required for DSH Shell isolation`**：未安装 `bwrap`，或内核禁用非特权 user namespace。
3. **`DSH 0.2.2-rc.1 is not installed` 或路径错误**：检查构建传入的 `--dsh-bin` 是否真实存在且不在 `/tmp` 下，`runtime.json` 中的 Node 路径对部署用户有可读可执行权限。
4. **云服务器上 QQ / 企业微信 Gateway 连不上**：
   - QQ 放行 `bots.qq.com`（WSS/HTTPS 443）；企业微信放行 `openws.work.weixin.qq.com`（WSS 443）与企微临时媒体 HTTPS 下载域名；
   - 注意企业微信同一 `WECOM_BOT_ID` 同时只允许**一个在线 WebSocket 长连接**，如切新环境会被 `disconnected_event` 踢下线，请勿对同一 BotID 在多台服务器重复启动 `company-agent-wecom.service`；
   - 查看实时日志：
   ```bash
   journalctl -u company-agent@admin.service -u company-agent-qq.service -u company-agent-wecom.service -u company-agent-superagent.service -n 100 --no-pager
   ```
