---
name: 测试基础设施
description: 提供端到端测试所需的基础能力脚本，包括日志拦截提取、数据库快照对比、副作用 mock 和验证。
---

# 测试基础设施

## 描述

提供端到端测试所需的基础能力脚本，供 container_verification_agent 编排下的 L3 执行者调用。

## 脚本

### log_interceptor.py
日志拦截 + 按规则提取匹配。

**调用方式**：
```bash
# 拦截运行时日志并按规则提取
python scripts/log_interceptor.py --log-file <日志文件路径> --rules <规则JSON文件> [--output <输出路径>]

# 实时拦截（启动后在后台监听，捕获指定进程的输出）
python scripts/log_interceptor.py --follow --log-file <日志文件路径> --rules <规则JSON文件> [--output <输出路径>]
```

**规则文件格式**（JSON）：
```json
{
  "rules": [
    {"name": "api_error", "pattern": "ERROR.*api.*\\d{3}", "level": "ERROR"},
    {"name": "task_complete", "pattern": "task.*completed.*id=([\\w]+)", "extract_groups": true}
  ]
}
```

### db_snapshot.py
数据库快照对比（操作前后 diff）。

**调用方式**：
```bash
# 创建快照
python scripts/db_snapshot.py create --tables <表名,逗号分隔> --output <快照输出路径>

# 对比两个快照
python scripts/db_snapshot.py diff --before <快照1路径> --after <快照2路径> [--output <对比结果输出路径>]
```

### side_effect_mock.py
通知/消息等副作用 mock 和验证。

**调用方式**：
```bash
# 启动 mock 服务，记录所有副作用
python scripts/side_effect_mock.py serve --port <端口> [--output <记录输出路径>]

# 验证已记录的副作用是否符合预期
python scripts/side_effect_mock.py verify --record <记录文件路径> --expect <预期JSON文件> [--output <验证报告路径>]
```

## 使用场景

- 端到端测试中拦截日志，验证关键事件是否触发
- 数据库操作前后快照对比，验证数据变更是否符合预期
- Mock 外部通知服务，验证副作用（消息推送、邮件发送等）是否正确触发

## 依赖

- Python 3.10+
- 标准库（json, re, sqlite3, argparse, http.server 等）
- 无额外第三方依赖
