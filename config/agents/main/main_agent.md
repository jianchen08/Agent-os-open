# 灵汐主Agent提示词 — 容器workspace决策规则补充

> 本文件记录对灵汐主Agent（lingxi.yaml）提示词的workspace决策规则修改，防止容器任务误填workspace。

## 容器任务 workspace 决策规则

在「容器任务流程」步骤2「创建容器」中，补充以下 workspace 决策规则：

1. **一般情况下（全新项目）→ 不填 workspace**
2. **仅当用户明确指定要修改某个已有项目目录时 → 填该目标项目目录**
3. **⚠️ 禁止填写系统内部路径（container_xxx），那只用于资源管理任务**

## workspace 参数表格（容器任务描述更新）

原描述：「指向目标项目目录，系统复制到容器空间」

新描述：「全新项目→不填；已有项目→指向目标项目目录」

## 修改文件

- 实际修改文件：`config/agents/main/lingxi.yaml`（system_prompt 字段 + static_vars 工作空间机制）
- YAML 格式验证通过（0 errors, 0 warnings）
