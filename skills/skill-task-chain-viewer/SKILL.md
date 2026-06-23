---
name: 任务链可视化
description: 将 docs/tasks/ 下的任务文件和 .project/ 下的项目文档汇总为单个自包含 HTML，供人类审阅任务链全貌、依赖关系和 AC 追溯覆盖。在方案规划完成后、交付用户审阅前使用。
---

# 任务链可视化

## 描述

读取方案规划阶段产出的任务文件（`docs/tasks/task_XX_*.md`，含 YAML frontmatter）
和项目文档（`.project/*.md`），生成一个**自包含**的交互式 HTML 页面（所有
CSS/JS 内联，无外部依赖，可离线打开），供用户在方案确认前审阅：

- 任务全貌与状态分布
- 任务间依赖拓扑（可点击节点跳转详情）
- 验收标准的追溯覆盖（方案级 AC → 哪些任务级 AC 覆盖，暴露「无人负责的 AC 死角」）

## 脚本

### generate_task_chain_html.py

主脚本。解析任务文件 frontmatter，渲染 HTML。

调用方式：

```bash
python skills/skill-task-chain-viewer/scripts/generate_task_chain_html.py --title "项目名称"
```

参数：

| 参数 | 必填 | 说明 |
|------|------|------|
| `--title` | 否 | 页面标题，默认「项目任务链」 |
| `--tasks-dir` | 否 | 任务文件目录，默认 `docs/tasks/` |
| `--project-dir` | 否 | 项目文档目录，默认 `.project/` |
| `--output` | 否 | 输出路径，默认 `docs/working/{title}_task_chain.html` |

## 使用场景

- 方案规划完成后，提交给用户确认前，生成可视化让用户一眼看清任务结构和 AC 覆盖
- 执行过程中重新生成，查看任务状态进展
- 发现 AC 追溯死角（某条方案级 AC 没有任何任务级 AC 覆盖）

## 注意

- 任务文件必须包含 YAML frontmatter（`task_id`/`task_name`/`executor`/`depends_on`/`status`），
  否则该任务会被标记为「格式缺失」并跳过详情渲染
- 依赖拓扑用 SVG 内联绘制，不依赖任何 CDN 或外部库
