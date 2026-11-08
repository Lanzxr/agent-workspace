# ADR 0002 使用自定义模型 Provider Route 接入企业网关

状态：已接受
日期：2026-11-08

## 背景

项目现有直接 HTTP 调用使用 `AGENT_MODEL_URL` 和 `AGENT_MODEL_API_KEY`。同一地址的 `/models` 返回了可用模型，使用 `deepseek-v4.1-flash` 的 OpenAI Chat Completions 请求返回 HTTP 200。

DSH 使用自己的 Provider Route。仅设置 `DEEPSEEK_BASE_URL` 和 `DEEPSEEK_API_KEY` 会继续走内置 `deepseek-official` provider，不能表达企业网关的自定义协议、模型目录和兼容性设置。

## 决策

DSH 接入企业网关时创建独立的 Custom Model Provider：

- API protocol 使用 `openai-completions`。
- Base URL 使用部署配置中的模型基础地址。
- API key 只通过环境变量引用，不写入 profile 或 Git。
- Model catalog 以网关 `/models` 返回的模型 ID 为准。
- 当前已验证模型 ID 为 `deepseek-v4.1-flash`。
- DSH 的默认模型选择显式指向这个 Provider Route 和模型 ID。

项目自己的直接 HTTP 适配器和 DSH Provider Route 是两个独立边界。一个调用链成功不能替代另一个调用链的验收。

## 结果

模型连接配置由安装器或运行时配置生成，仓库只保存不含密钥的模板和契约。未来更换网关时，只替换 Provider Route，不改变 DSSuperAgent、控制面或 Agent Workspace。
