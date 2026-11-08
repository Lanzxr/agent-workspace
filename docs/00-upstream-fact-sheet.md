# 上游事实核验表

本文件只记录实际检查过的上游事实。未核验项目保持 `unknown`，不能因为名称或示例代码看起来相似就标记为可用。

## DSSuperAgent

| 项目 | 状态 |
|---|---|
| 真实源码仓库 | unknown，待 Step 0 核验 |
| 启动方式 | unknown |
| 静默观察入口 | unknown |
| 工作计划输出 | unknown |
| 执行意图输出 | unknown |
| 会话恢复方式 | unknown |
| 持久化方式 | unknown |
| 许可证 | unknown |

## DSH

| 项目 | 状态 |
|---|---|
| 真实源码或发行包 | unknown，待 Step 0 核验 |
| 会话执行入口 | unknown |
| 工具调用入口 | unknown |
| Workspace 注入方式 | unknown |
| 结果回传格式 | unknown |
| 依赖和版本 | unknown |
| 许可证 | unknown |

## 核验规则

1. 先记录仓库、版本、许可证和依赖。
2. 再用最小真实调用验证输入、输出、错误和恢复。
3. 真实能力映射到对应契约。
4. 无法验证的能力记录为阻塞，不使用同名 HTTP 接口或规则模拟冒充。

