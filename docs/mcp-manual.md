# Company Agent 导入 MCP 与 Tool Forge 扩展手册

本手册说明如何在**不破坏 DSH `bash` 沙箱断网（`--unshare-net`）与零密钥原则**的前提下，将外部业务系统、数据库或定制脚本能力安全接入 Company Agent 并按需分享给员工群使用。

---

## 一、安全架构与扩展方式选型

Company Agent 中的第三方业务访问与密钥**全部隔离在 DSH 主进程外部或 Tool Forge 宿主机回调代理内**，模型执行 `bash` 阶段无法联网也读取不到任何环境变量密钥：

```text
QQ 群 / 私聊
  └─► DSH Worker（模型主体，按会话层级挂载授权的 MCP 客户端）
        ├── mcp__<外部MCP>__*        ──► 外部独立 MCP 进程/HTTP 服务（密钥留在 runtime.env）
        ├── mcp__forge_<层级>__*     ──► Tool Forge MCP :28768（密钥留在 forge-secrets.json 0600）
        └── bash (bwrap --unshare-net + --unsetenv 擦除全部密钥)
              └── 仅可离线运行 shared-tools/{common,internal} 脚本处理工作区文件
```

### 三种扩展路径怎么选？

| 需求类型 | 推荐路径 | 适用说明 | 生效方式 |
|---|---|---|---|
| **已经有成熟的 MCP Server**（如独立仓库维护的大型业务 MCP、`stdio` 命令行或远程 HTTP MCP） | **路径 A：导入外部 MCP** | 配置到 `COMPANY_AGENT_MCP_SERVERS` 或管理台 `/api/mcp-servers`，由 `controlled-mcp.mjs` 桥接加载 | 控制台开关立即按任务生效；新静态环境变量需更新 `runtime.env` 后重启相关服务 |
| **需要临时接入外网 HTTP API / 分页聚合 / 签名请求**，想直接让 AI 在管理员对话里帮着写好给员工用 | **路径 B：Tool Forge 对话锻造** | 管理员私聊用 `/forge-domain` + `/forge-secret` 授权后，AI 探针试跑 → 写 `http_api` 或 `python_plugin` → 测试 → 审批上线 | `/forge-approve` 后**零重启即时热生效** |
| **纯本地离线数据处理脚本**（无需外网、无需 API 密钥，如复杂表格/文本计算） | **路径 C：`shared-tools` 共享脚本**（也可通过 Tool Forge `shared_script` 审批写入） | 放在 `shared-tools/{common,internal}/`，由 `bwrap` 只读挂载到沙箱内执行 | 写入/批准后下次任务直接可用 |

---

## 二、路径 A：导入外部现成 MCP 服务

### 2.1 支持的传输协议与配置字段

通过 [mcp_registry.py](file:///D:/work/AI工作文件夹/内部agent/company-agent/app/mcp_registry.py) 进行严格白名单校验，支持两种传输模式：

1. **`stdio`（宿主机子进程命令）**：适合本机部署的 Python / Node / Go MCP 可执行程序。
   - 必填字段：`server_id`、`transport: "stdio"`、`access_level`（`admin` / `internal` / `common`）、`description`、`command`、`args`
   - 密钥引用：`env_refs`（`{"子进程目标环境变量名": "COMPANY_AGENT_MCP_SECRET_..."}`）
2. **`streamable-http`（HTTP/HTTPS MCP 端点）**：适合独立部署的微服务 MCP（支持 WSL2 访问 Windows 宿主服务）。
   - 必填字段：`server_id`、`transport: "streamable-http"`、`access_level`、`description`、`url`
   - 密钥引用：`header_env_refs`（例如 `{"Authorization": "Bearer ${COMPANY_AGENT_MCP_SECRET_...}"}`）

### 2.2 密钥托管规则（严禁把明文写进配置/控制台）

- 所有外部服务密钥**必须写在 `runtime.env` 环境变量里**（建议统一以 `COMPANY_AGENT_MCP_SECRET_` 命名，切勿复用平台保留字段如 `AGENT_MODEL_API_KEY`、`QQBOT_CLIENT_SECRET`、`COMPANY_AGENT_ADMIN_TOKEN` 等，控制面会直接拒绝）。
- 控制台 `/api/mcp-servers` 和 JSON 配置里**只写环境变量名引用**；未被引用的环境变量绝不会进入该层级的 DSH 桥接进程，并且所有已注册的密钥名在启动 `bwrap` 时全部列入 `--unsetenv` 擦除清单。

### 2.3 导入操作示例

#### 示例 1：在 `runtime.env` 静态导入独立本地 MCP（`admin` 级）

例如你本机另外起了一个监听 `127.0.0.1:18766/mcp` 的只读查询服务，仅开放给管理员使用：

```dotenv
# 在 runtime.env 中配置密钥与受控 MCP 定义
COMPANY_AGENT_MCP_SECRET_BIZ_RO=<替换成实际Bearer令牌>
COMPANY_AGENT_MCP_SERVERS=[{"server_id":"biz_ro","description":"业务系统只读 MCP 查询","transport":"streamable-http","access_level":"admin","url":"http://127.0.0.1:18766/mcp","header_env_refs":{"Authorization":"Bearer ${COMPANY_AGENT_MCP_SECRET_BIZ_RO}"}}]
```

修改 `runtime.env` 后，在 WSL 执行一次同步并重启服务使新增环境变量加载生效：
```bash
sudo systemctl restart company-agent.target
```

#### 示例 2：从 WSL2 接入 Windows 宿主机上的 MCP（`wsl-host` 自动桥接）

明文 `http://` MCP 除 `127.0.0.1`/`localhost` 外，系统还原生识别 **`wsl-host`**（[mcp_registry.py](file:///D:/work/AI工作文件夹/内部agent/company-agent/app/mcp_registry.py#L18) 及 [dsh.py](file:///D:/work/AI工作文件夹/内部agent/company-agent/app/dsh.py) 会在运行时动态解析 WSL2 默认宿主机网关 IP）：

```json
{
  "server_id": "win_erp_ro",
  "description": "Windows 宿主内网 ERP 只读接口（仅内部群与管理员可用）",
  "transport": "streamable-http",
  "access_level": "internal",
  "url": "http://wsl-host:9001/mcp",
  "header_env_refs": {
    "X-API-Key": "${COMPANY_AGENT_MCP_SECRET_ERP}"
  }
}
```

> [!NOTE]
> 对外网远程 MCP 服务器，系统**强制要求使用 `https://`**，禁止对非本机/非网桥地址使用明文 `http://`。

#### 示例 3：通过 Web 控制台热调整范围、停用或加载配置

打开 `http://127.0.0.1:28765`（实际端口以 `COMPANY_AGENT_ADMIN_PORT` 为准）→「**共享工具与组件**」→「受控 MCP 服务」：
- 可以随时切换某服务的 `enabled` 启用/停用、调整 `access_level`（`admin` → `internal` → `common`）或修改描述；
- 若新增了引用新密钥的 MCP，须先将密钥环境变量写入 `runtime.env` 并重启服务，再在控制台编辑引用；页面会实时用绿色/红色标签提示密钥是否就绪。

---

## 三、路径 B：用 Tool Forge 在管理员对话中锻造并分发工具

如果外部系统**只有普通 REST/HTTP API（还没有现成 MCP）**，或者希望把“多页流水拉取 + Python 精确求和聚合”打包成一个可靠工具，按下面 4 步即可在**同一条管理员 QQ 私聊**中完成注册与上线：

### 3.1 第一步：放行目标域名与托管凭据（管理员私聊命令）

```text
/forge-domain add api.example.com
/forge-secret set BIZ_API_KEY sk-example-key-placeholder
```

- 如需同时放行多级子域，使用 `/forge-domain add example.com --subdomains`。
- 如果是内网 IP 业务系统（如 `192.168.10.25`），**必须将该 IP 精确加入白名单**才会放行；`127.0.0.1`、`localhost` 与 `169.254.169.254` 等元地址永远禁止访问。
- 密钥输入后会由平台在入库与转发给上游前**自动替换为 `[REDACTED]`**，模型看不到明文。

### 3.2 第二步：让 Agent 在管理员私聊探针试跑并起草工具

管理员直接在 QQ 私聊向机器人发自然语言指令即可。系统会通过 `forge_admin`（`:28768/mcp/forge`）自动执行：

1. **`forge_list_capabilities`**：先查可用域名白名单与密钥别名列表（避免写错变量名）；
2. **`forge_probe_http`**：使用 `https://api.example.com/...` 和 `{{BIZ_API_KEY}}` 模板实际试调一次接口，确认分页结构与字段命名（响应里若含敏感值会自动脱敏）；
3. **`forge_draft_tool`**：根据接口复杂度二选一（也支持 `shared_script`）：
   - **`http_api`（声明式单接口模板）**：
     ```json
     {
       "tool_id": "query_user_logs",
       "kind": "http_api",
       "description": "按用户与日期查询请求日志摘要",
       "when_to_use": "需要按天检索指定用户的请求明细时调用",
       "when_not_to_use": "不需要联网查询日志时勿调用",
       "operation_type": "read",
       "target_visibility": "internal",
       "input_schema": {
         "type": "object",
         "properties": {
           "user": {"type": "string", "description": "目标用户名"},
           "days": {"type": "integer", "description": "查询最近几天"}
         },
         "required": ["user", "days"]
       },
       "http_spec": {
         "method": "GET",
         "url_template": "https://api.example.com/v1/logs",
         "headers": {"Authorization": "Bearer {{BIZ_API_KEY}}"},
         "query_template": {"user": "{{args.user}}", "days": "{{args.days}}"},
         "response_path": "data"
       },
       "allowed_domains": ["api.example.com"],
       "default_test_input": {"user": "demo", "days": 7}
     }
     ```
   - **`python_plugin`（受限聚合/翻页/签名脚本）**：
     适合解决“平台无聚合接口、需要多页拉取并在宿主机端精准统计”的问题。脚本须定义 `def run(args, ctx):`：
     - **网络调用**：只能走 `ctx.http.get(...)` / `ctx.http.post(...)` / `ctx.http.request(...)`（仍受域名白名单及 SSRF 防护校验，密钥用 `{{SECRET_ALIAS}}` 写法由宿主机替换）；
     - **签名运算**：可用 `ctx.hmac_sha256("SECRET_ALIAS", msg)` 在宿主机回调侧计算 HMAC 摘要（密钥明文绝不进入脚本进程）；
     - **安全禁止项**：静态 AST 检查禁止 `os`、`sys`、`subprocess`、`socket`、`urllib.request`、`http`、`eval`/`exec`/`open`/`__import__` 及一切双下划线属性访问；仅放行 `json`、`math`、`statistics`、`collections`、`datetime`、`decimal`、`hashlib`、`hmac`、`re`、`csv`、`urllib.parse` 等纯计算与文本处理标准库。
4. **`forge_test_tool`**：在隔离执行环境实测一次，生成绑定当前代码 `code_hash` 的脱敏测试报告；
5. **`forge_submit_release`**：提交发布提案，输出 `proposal_id`。

### 3.3 第三步：管理员确认上线与跨层分发

- AI 无法自批新工具，必须在 **管理员 QQ 私聊** 回复：
  ```text
  /forge-approve <proposal_id>
  ```
  或在 Web 控制台「共享工具与组件 → 工具发布审批队列」审阅代码预览及测试输出后点击「批准上线」。
- 按提案里的 `target_visibility`：
  - `admin`：仅挂到 `forge_admin`，供管理员使用；
  - `internal`：同时热挂到 `forge_admin` 与 `forge_internal`，**所有 L2 内部群与绑定员工立即可用**；
  - `common`：热挂到全部层级（包含开启权限的 L1 客户群，慎选）。

### 3.4 第四步：版本维护、排查与回滚

- **查看工具运行状态**：管理员私聊发 `/forge-tools`，或在控制台查看成功调用次数、总次数及平均耗时（来自 `forge_call_logs`）。
- **无停机迭代升级**：改版时直接再起草同名 `tool_id`（自动成为 `v2` 草稿）并测试，期间在线员工调用的仍然是稳定版 `v1`；管理员 `/forge-approve` 新提案后才瞬间切换到 `v2`。
- **紧急停用与版本回滚**：
  - 停用：`/forge-disable <tool_id>`
  - 回滚上一已审批版或指定版：`/forge-rollback <tool_id>` 或 `/forge-rollback <tool_id> v1`

---

## 四、自研独立 MCP 服务兼容规范（接入必读）

如果你在外部独立开发了一个新的 Streamable HTTP MCP 服务准备通过 **路径 A** 导入，请务必满足与官方 `@deepseek-ai/dsh-mcp-client` / `@deepseek-ai/dsh-tools` 对接的协议要求：

1. **JSON Schema 严格子集约束**（极重要）：
   - DSH 工具注册校验器**仅允许**：关键字 `type`、`oneOf`、`properties`、`required`、`additionalProperties`、`items`、`enum`、`const`，以及注释字段 `description`、`title`、`default`、`examples`。
   - **禁止**在 `inputSchema` 中直接出现 `$schema`、`minimum`、`maximum`、`minLength`、`maxLength`、`format`、`anyOf` 等关键字，否则 DSH 客户端注册该 MCP 工具时会报错拒绝（注：Tool Forge 内置端点已通过 `_project_mcp_input_schema` 自动转换，但你自己独立开发的 MCP 需自行遵守此子集）。
2. **Streamable HTTP 传输行为**：
   - `POST <endpoint>` 处理 `initialize`、`ping`、`tools/list`、`tools/call` 并返回 `application/json`（JSON-RPC 2.0）；
   - 对于 JSON-RPC 通知（如 `notifications/initialized`，无 `id` 字段），返回 `HTTP 202 Accepted` 空响应；
   - 如服务端不主动推送 SSE，对 `GET <endpoint>` 返回 `405 Method Not Allowed`（`Allow: POST, DELETE`）。
3. **只读 / 写操作拆分建议**：
   - 不要把“管理员删库/全量修改接口”和“员工常用只读查询接口”放在同一个高风险服务中一并暴露给 `internal` 层；建议按能力粒度拆分端点或使用 `access_level: "admin"` 隔离高危服务。
