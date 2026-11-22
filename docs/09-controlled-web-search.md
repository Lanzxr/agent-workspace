# 受控联网检索

DSSuperAgent 的联网搜索不依赖上游大模型网关账号，也不调用需要 DeepSeek 搜索密钥的 DSH 搜索提供方。模型继续由 `AGENT_MODEL_URL`、`AGENT_MODEL_NAME` 和 `AGENT_MODEL_API_KEY` 配置；搜索由自托管 SearXNG 执行。

DSSuperAgent 主会话没有联网工具；需要实时信息时由它派发 worker 任务。worker 只注册 `orchestrator_web_search` 和 `orchestrator_web_fetch` 受控工具，不注册 DSH 原生 `tool-web`。Company Agent 校验桥接密钥和控制台中的 `dssuperagent_web_search_enabled`；开关关闭则拒绝，开启才调用本机 SearXNG JSON API。网页读取也先经过同一开关，再由 DSH HTTP fetch 执行。控制台可即时开关；审计日志保存查询哈希而不保存原始搜索文字。DSH 核心未修改。

Company Agent 事件的当前消息是联网需求判断的依据。若当前消息要求最新、实时、外部或网页信息，DSSuperAgent 必须提出 `dsh.task`，不能因为主会话没有联网工具就直接返回“无法联网”。QQ 渠道若需要回复，发布建议使用 `group_update`；如果模型错误选择 `private_nudge`，Company Agent 在群聊发布已获允许时会记录降级原因并转为群内回复，防止结果被静默丢弃。

SearXNG 默认监听 `127.0.0.1:28770`（随 `COMPANY_AGENT_PORT_BASE` 或 `COMPANY_AGENT_SEARCH_PORT` 派生），JSON 输出由 `deploy/search/settings.yml` 显式启用，镜像固定到已验证的 digest。它不运行模型或嵌入服务。SearXNG 使用固定 Docker 网桥 `br-agent-search`；`anythingllm-egress-firewall.service` 只允许 AnythingLLM 和 SearXNG 网桥向外建立 TCP 443 连接并允许已建立连接的回包，不开放容器入站端口。秘密 `SEARXNG_SECRET` 保存在 WSL 私有配置文件 `~/.config/company-agent/search.env`，不要提交到仓库。项目运行配置项如下：

```text
COMPANY_AGENT_WEB_GATE_OWNER=admin
COMPANY_AGENT_KNOWLEDGE_API_OWNER=admin
COMPANY_AGENT_WEB_GATE_HOST=127.0.0.1
COMPANY_AGENT_WEB_GATE_PORT=28767
COMPANY_AGENT_WEB_GATE_URL=http://127.0.0.1:28767/internal/web/authorize
COMPANY_AGENT_WEB_SEARCH_URL=http://127.0.0.1:28770/search
COMPANY_AGENT_WEB_SEARCH_TIMEOUT_SECONDS=20
```

`COMPANY_AGENT_WEB_GATE_OWNER` 决定哪个 Company Agent 进程监听联网权限接口；同一时刻只能有一个进程监听，常规部署由管理服务 `admin` 承载。控制台的 DSSuperAgent 联网开关立即生效，无需重启 Agent。DSH 执行任务自己的联网开关仍独立。

启动和检查 SearXNG：

```bash
docker compose --project-name company-agent-search --env-file ~/.config/company-agent/search.env -f deploy/search/compose.yaml up -d
sudo systemctl restart anythingllm-egress-firewall.service
curl 'http://127.0.0.1:28770/search?q=检索测试&format=json'
```

失败时：

- 控制台开关关闭或请求未被允许：DSSuperAgent 返回 `webSearchDisabled`。
- Company Agent 网关不可达：检查 `28767` 端口（或 `COMPANY_AGENT_WEB_GATE_PORT`）、网关所有者及桥接密钥。
- SearXNG 不可达或响应错误：检查 `28770` 端口（或 `COMPANY_AGENT_SEARCH_PORT`）、容器健康与日志；Company Agent 以失败状态返回，不会编造搜索结果。
- SearXNG 返回空结果：检查 `company-agent-search-network` 是否绑定到 `br-agent-search`，并确认 `DOCKER-USER` 有该网桥到默认外网网卡的 TCP 443 放行和回包规则。SearXNG 是独立网桥，AnythingLLM 的出站规则不会自动覆盖它。
- 结果中有不可响应或 CAPTCHA 的搜索引擎时，SearXNG 仍会合并其他实际返回结果的引擎；每条来源保留实际 engine 字段供检查。

## 模型/搜索问题排查

模型调用成功只说明上游模型链路可用，不代表搜索链路可用。搜索是否真正完成，应同时核验控制台开关、Company Agent 审计记录、SearXNG 返回的来源 URL 和 engine。不得因搜索失败就把模型 API 切换到 DeepSeek，也不要把开关 200、进程健康或模型正常误当成搜索结果。


## 构建与运行版本

改动必须在 `company-agent` 和 `dssuperagent-company-agent-step4` 源码目录完成。先构建并测试 Company Agent 与 DSSuperAgent bundle，再运行 `scripts/build-release.py` 生成带源码提交、脏文件清单和产物 SHA-256 的发布包。WSL 运行服务从 `/home/lan_z/company-agent-deploy/current` 的不可变发布目录启动，不从旧源码副本或临时目录运行。`company-agent@admin` 持有权限网关；`company-agent@superagent` 和 `company-agent@qq` 顺序依赖它。运行配置保存在 `~/.config/company-agent/runtime.env`，任务数据库和 DSH state 不随版本替换。
