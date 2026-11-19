# 长期资料库与 AnythingLLM 接入

## 目标与职责

AnythingLLM 作为自托管资料库平台，负责文件解析、分块、嵌入、OCR 和检索；大语言模型及嵌入模型均由外部服务提供，部署文件不启动本地模型。Company Agent 是权限边界，负责 QQ 附件下载、身份与会话范围校验、原件保存、资料库调用、审计和 DSH 文件交付。DSSuperAgent 通过 Company Agent 的只读接口检索，可依据证据继续多次查询。DSH 不连接 AnythingLLM，也不持有资料库凭证或路径。

```text
'QQ 附件
  -> Company Agent 立即登记稳定 attachment_id 和占位状态
  -> 先下载并校验原件，保存为 stored（不等待 OCR）
  -> DSSuperAgent 看到当前消息/会话历史和附件状态，可读取原件或等待资料库结果
  -> OCR/解析和脱敏索引在后台进入同一个私有 AnythingLLM 工作区
  -> Company Agent 分别登记每个消息来源及其权限范围
  -> DSSuperAgent 通过短期授权查询，取得片段、文件名、位置和来源，并明确选择 document_id 或 attachment_id
  -> Company Agent 在执行前再次核验提案中的 ID
  -> 获准后把原件复制并校验哈希，再交给 DSH 工作区
'```

## 权限与敏感文件

群聊只能查询同一群聊登记的文件；一对一会话只能查询同一私聊登记的文件；配置在 `COMPANY_AGENT_KNOWLEDGE_ADMIN_USER_IDS` 中的内部管理员可以查询全部范围。Company Agent 在服务端依据来源数据库过滤结果，不把 AnythingLLM 工作区名称当权限控制。同一内容只存一份原件和索引，但每次上传都有独立来源记录和权限范围，检索及执行交付均重新校验。

文件名为 `.env` 或 `.env.*` 时标记为敏感，索引副本中的所有赋值内容会替换成 `[REDACTED]`。其他文本/配置文件中识别到密钥字段时也会脱敏；无法安全脱敏时拒绝索引。原始字节仅保存在 Company Agent 私有资料目录，不进入搜索响应或日志。管理员若明确授权任务，可以按相同文件 ID 检查规则把敏感原件交给 DSH。

## 图片、扫描 PDF 与 500 MiB 目标

PNG、JPG 和 JPEG 会按内容签名识别，保存原件、计算 SHA-256，并以原始图片上传到 AnythingLLM。扫描版 PDF 也保留原件并直接交给 AnythingLLM；文字型 PDF 继续由 AnythingLLM 的普通 PDF 解析器处理。Company Agent 不在本地猜测 OCR 结果，因此上传、解析或 OCR 失败时资料状态保持 `failed`，不会变成可检索的 `ready`。

AnythingLLM v1.19.0 使用 `tesseract.js`，上游镜像本身不预置中文 `chi_sim` 语言模型；语言模型会写入 `STORAGE_DIR/models/tesseract`。Compose 增加了 `ocr-assets` 预热服务，读取 `ANYTHINGLLM_OCR_LANG`（默认 `chi_sim,eng`），在主服务启动前把语言包缓存到持久化卷。默认只允许这两个已验收语言；如需增加语言，需同时更新预热服务白名单和对应测试。下载地址可通过 `ANYTHINGLLM_OCR_DATA_BASE_URL` 替换为内部镜像。

部署时使用：

```powershell
docker compose --env-file <私有运行目录>/anythingllm.env up -d
```

首次启动会先完成 OCR 语言包预热；预热失败会阻止 AnythingLLM 启动，避免服务运行但中文图片和扫描 PDF 无法解析。

## 格式和 500 MiB 目标

目标格式包括 PDF、DOC/DOCX、PPT/PPTX、XLS/XLSX、TXT、Markdown、CSV、JSON、YAML、TOML、INI、CONF、CFG、ENV、LOG、XML、HTML、SQL、脚本和常见源码文本。接收时校验内容签名和文本编码，不只依赖扩展名。文本/配置类索引副本统一以 `.txt` 后缀提交，让 AnythingLLM 选择纯文本解析器；原文件名和字节不变。旧版 Office 二进制类型仍需扩展名辅助区分。各类 Office 文档的实际解析能力需在部署版本上验收。

Company Agent 单文件上限为 524,288,000 字节（500 MiB）。Nginx 入口的目标上限是 512 MiB，以容纳 multipart 开销；但 Nginx 配置文件未能加入当前仓库。已核对 AnythingLLM v1.19.0 的上传中间件使用 Multer 磁盘存储，源码中没有显式的 `fileSize` 限制。这只能说明未发现应用层固定上传上限，不能证明解析器可以处理 500 MiB 文件，也不能证明服务器有足够临时磁盘和内存。当前没有完成真实大文件解析，因此 500 MiB 仍是目标，未获真实验收。不能静默降低目标；部署时需贯通 QQ 下载、Company Agent、反向代理和 AnythingLLM 上传、解析、检索，并验证 DSH 收到的原件哈希。


## 上游版本和接口核验

截至 2026-11-17，AnythingLLM 官方 GitHub 最新发布版为 v1.19.0。该版本源码定义了需 API Key 的工作区列表和创建接口、multipart `POST /api/v1/document/upload`（字段为 `file`、逗号分隔的 `addToWorkspaces` 和 JSON 字符串 `metadata`），以及 `POST /api/v1/workspace/:slug/vector-search`。上传响应包含文档位置和处理成功状态。`GET /api/v1/document/:docName` 返回文档元数据，不返回原文件字节。因此 Company Agent 从自己的私有存储交付哈希校验后的原件。本机已真实验证健康接口、Developer API 认证、工作区列表和工作区创建；上传、解析及向量检索仍未通过验收。

## 部署 AnythingLLM

`deploy/anythingllm/docker-compose.yml` 固定使用 AnythingLLM v1.19.0，持久化存储卷并只绑定本机回环地址；它设置聊天和嵌入引擎为 OpenAI 兼容的外部服务，不包含本地模型容器。将 `deploy/anythingllm/anythingllm.env.example` 复制到私有运行目录并填写外部服务配置，再以该文件作为 Compose env 文件启动：

```powershell
docker compose --env-file <私有运行目录>/anythingllm.env up -d
```

聊天模型和嵌入模型可以使用不同的外部服务；嵌入服务必须提供与 AnythingLLM generic-openai embedder 兼容的 `/embeddings` 接口。不要把只支持聊天补全的模型端点误当成嵌入服务。可选的 `ANYTHINGLLM_HTTP_PROXY`、`ANYTHINGLLM_HTTPS_PROXY` 和 `ANYTHINGLLM_NO_PROXY` 供 AnythingLLM 容器访问外网时使用。反向代理 Nginx 配置尚未加入仓库；部署到接收大文件的入口前，必须将 `client_max_body_size` 设为至少 `512m`，并把客户端请求体、代理发送及读取超时设为足以完成上传和解析的时长。

### WSL2 Docker 出站网络

AnythingLLM 需要向外部嵌入 API 发起 HTTPS 请求，也需要从配置的 OCR CDN 下载语言包；受控联网搜索使用独立的 SearXNG Docker 网桥，也需要对外发起 HTTPS 搜索请求。实际故障中，AnythingLLM 容器对 `api.siliconflow.cn:443` 的连接超时；虽然 systemd 防火墙服务显示运行过，旧网桥却没有对应的出站放行规则。旧脚本把规则写进 Docker 自己管理的链，网络重建后规则可能消失。只看服务状态不足以证明容器能出站。

仓库提供 `deploy/anythingllm/apply-egress-firewall.sh`，规则写入 Docker 保留给用户管理的 `DOCKER-USER` 链，只允许 AnythingLLM 和受控搜索网桥向 WSL 默认外网网卡建立 TCP 443 连接，并允许已建立连接的回包。SearXNG 使用固定网桥 `br-agent-search`，与 AnythingLLM 的 `br-agent-kb` 规则分别匹配。该规则不会开放新的主机入站端口，也不会放行容器到其他 Docker 网段的转发。Compose 将网桥固定为 `br-agent-kb`，让重建容器或默认网络后规则仍匹配。首次部署时，在 WSL 的 Ubuntu-24.04 中、仓库目录下安装并启用规则：

```bash
sudo install -m 0755 deploy/anythingllm/apply-egress-firewall.sh /usr/local/sbin/company-agent-anythingllm-egress
sudo install -m 0644 deploy/anythingllm/anythingllm-egress-firewall.service /etc/systemd/system/anythingllm-egress-firewall.service
sudo systemctl daemon-reload
sudo systemctl enable --now anythingllm-egress-firewall.service
```

若修改了网桥名称或 WSL 默认外网网卡，重新运行 `sudo systemctl restart anythingllm-egress-firewall.service`。容器内访问外部 `/v1/models` 未带 API Key 时，收到 HTTP 401 只证明 TCP/TLS 已连通；只有实际嵌入请求成功、AnythingLLM 文档状态变为可检索，才证明完整入库成功。

## 本机部署验收记录

AnythingLLM v1.19.0 已通过 Compose 启动，容器健康检查为 `healthy`，`/api/ping` 返回 `200`。Company Agent Developer API Key 已在本机服务中创建并保存在仓库外的私有运行配置中；带 Key 的工作区列表和 `company-agent-library` 工作区创建请求通过。未认证的工作区请求返回 `403`，符合 API 的认证要求。

外部嵌入模型配置已提供。本次从 AnythingLLM 容器向 `api.siliconflow.cn` 发出带配置密钥的真实 `/v1/embeddings` 请求，返回 HTTP 200；向 OCR CDN 下载地址的 HTTPS 请求也返回 HTTP 200。AnythingLLM `/api/ping` 返回 HTTP 200，容器状态为 `healthy`。本次没有重测工作区聊天接口。

Company Agent 的私有 Windows 运行配置已写入 AnythingLLM 地址、Developer API Key、授权随机密钥和 500 MiB 应用上限；当前运行中的 QQ 进程启动早于这些配置，尚未重启加载。DSSuperAgent 的活动 WSL 工作区含未提交改动，资料库扩展补丁无法直接应用到该工作区；本次没有覆盖或重置这些改动，也没有向 DSH 核心添加资料库访问。故 QQ 附件入库、DSSuperAgent 多轮查询、权限隔离及 DSH 原件交付尚未完成真实联调。

当前 OCR 验证状态：AnythingLLM v1.19.0 的真实 OCR 已用一张中文 PNG 和一份图片型中文扫描 PDF 验证。两次 Developer API 上传均返回 HTTP 200、`success=true`、一个解析文档和 `wordCount=21`；持久化解析结果均识别出“设备数量为 37 台”。`chi_sim.traineddata` 和 `eng.traineddata` 已缓存到 AnythingLLM 持久化卷。出站修复后，容器访问 OCR CDN 返回 HTTP 200，语言包预热可以在线下载。

真实向量检索已通过：中文 PNG 和扫描版 PDF 入库后，AnythingLLM 向量检索都找回“设备数量为 37 台”的内容；OCR 偶尔会在中文词间插入空格，验证时已先归一化空白。Company Agent 知识库模块的群聊范围隔离、管理员读取、越权拒绝及原件 SHA-256 校验也已通过验证。真实 QQ 收件下载和向 DSH 任务工作区交付不在本次出站及资料库验证范围内，仍需单独联调。

## Company Agent 配置

把仓库的 `runtime.env.example` 复制到私有配置文件并设置：

| 变量 | 用途 |
|---|---|
| `ANYTHINGLLM_BASE_URL` | AnythingLLM 服务地址，例如 `http://127.0.0.1:3001` |
| `ANYTHINGLLM_API_KEY` | Developer API Key |
| `ANYTHINGLLM_REQUEST_TIMEOUT_SECONDS` | 上传/查询请求超时，默认 180 秒 |
| `COMPANY_AGENT_KNOWLEDGE_GRANT_SECRET` | 至少 32 字节随机值，用于签发短期查询授权 |
| `COMPANY_AGENT_KNOWLEDGE_ADMIN_USER_IDS` | 可访问全部资料的内部用户 ID，逗号分隔 |
| `COMPANY_AGENT_KNOWLEDGE_MAX_FILE_BYTES` | Company Agent 文件上限，默认 524288000 |
| `COMPANY_AGENT_KNOWLEDGE_WORKSPACE_PREFIX` | 专用 AnythingLLM 工作区前缀 |
| `COMPANY_AGENT_KNOWLEDGE_API_HOST` / `_PORT` | DSSuperAgent 桥接监听地址/端口，默认 127.0.0.1:8766 |
| `COMPANY_AGENT_KNOWLEDGE_API_KEY` | 非回环监听时必需的 32 字节以上密钥；DSSuperAgent 也设置为相同值 |

AnythingLLM 地址、API Key 和授权密钥都填写后才启用；部分填写会阻止启动。凭据不能提交到 Git 或写入日志。DSSuperAgent 若运行在 WSL 或其他主机，桥接只绑定可达的私有地址并限制防火墙；跨不可信网络使用私有 HTTPS 隧道。

## RAGFlow 迁移影响

Company Agent 不再接受非空的旧 RAGFlow 环境变量；需改用上表的 AnythingLLM 变量，RAGFlow API Key 和数据集 ID 不能复用。数据库迁移会保留旧文档及来源行，并将引擎标为 `legacy_ragflow`；不会删除旧 RAGFlow 文件、服务、数据库或卷。旧索引不会自动复制。再次收到相同原件时，Company Agent 会按 SHA-256 复用文档记录，上传至 AnythingLLM 并更新该记录，同时保留旧、新来源权限记录。如果原件只存在 RAGFlow 中，需通过原有管理流程导出后重新入库；没有自动搬迁 RAGFlow 内容的功能。

## 失败状态和验证

下载、内容校验、上传或解析失败时，附件标记失败并把失败元数据交给判断流程，不会假称文件可检索。AnythingLLM 的错误日志只存错误类型，不存响应正文。DSH 交付前会复核原件 SHA-256；不匹配则停止交付。执行后的临时副本会清理，长期原件保留在 Company Agent 存储中。

自动化测试使用本地 HTTP 模拟服务和假客户端，覆盖群/私聊隔离、管理员读取、越权拒绝、同内容去重并保留多条来源、密钥脱敏、失败入库、短期授权、历史记录迁移和原件交付；这些测试不等于真实 AnythingLLM 联调。

后续验收需先配置一个外部嵌入服务的 `/embeddings` 地址、模型名和密钥，再验证聊天、文件上传、解析完成、向量检索、证据来源、Company Agent 原件读取、DSH 原件暂存和哈希校验、群/私聊/管理员隔离、敏感值脱敏，以及穿过 QQ 下载和 Nginx 的 500 MiB 边界文件解析与检索。DSSuperAgent 的知识查询扩展还需按活动工作区实际版本适配并验证。测试记录须区分模拟结果与真实 API 结果。


