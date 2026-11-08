# Agent Workspace 契约

## 目的

为 DSSuperAgent 和 DSH 提供一个持久、可恢复、可隔离的 Linux 工作环境。Workspace 是执行能力的边界，不是业务数据的最终状态源。

## 工作区结构

每个 Workspace 至少提供以下目录：

```text
workspace/
├── work/       # 当前任务输入和临时文件
├── tools/      # Agent 自建脚本和工具
├── artifacts/  # 可交付成果
├── logs/       # 执行日志
├── state/      # 工作环境状态和依赖清单
└── cache/      # 可清理的依赖缓存
```

## 生命周期

- `provisioned`：已创建身份、目录和基础依赖
- `ready`：可以接收执行请求
- `running`：至少有一个执行正在进行
- `suspended`：暂时停止但保留状态
- `recovering`：重启后恢复租约和文件状态
- `failed`：环境不可用，需要修复或重建
- `deleted`：按保留策略销毁

## 操作

Workspace Manager 必须提供：

- 创建和恢复 Workspace
- 获取能力和版本清单
- 执行前检查权限、限额和目录
- 保存输入、日志、成果和依赖清单
- 在重启后恢复未完成执行
- 导出可审计的 Workspace 摘要

## 隔离规则

- 默认禁止宿主机根目录和 Docker Socket。
- 生产访问、敏感数据和外发能力必须显式授予并带有效期。
- Workspace 中的普通脚本和依赖可以由 Agent 管理。
- 核心服务代码、权限策略和安装器文件不属于 Workspace 自主修改范围。

