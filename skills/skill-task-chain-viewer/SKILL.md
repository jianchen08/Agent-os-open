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
- AC 规模概览（正文里出现的 AC 编号总数、其中带 traces_to 追溯的条目数），
  用于快速感知覆盖规模。注意：本工具不读取方案文档的「方案级 AC 列表」，
  无法判断某条方案级 AC 是否无人覆盖——AC 死角分析需人工对照方案文档。

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

- 方案规划完成后，提交给用户确认前，生成可视化让用户一眼看清任务结构和 AC 覆盖规模
- 执行过程中重新生成，查看任务状态进展
- 感知 AC 追溯规模（带 traces_to 的 AC 数占比）。AC 死角分析需人工对照方案文档

## 注意

- 任务文件应包含 YAML frontmatter（`task_id`/`task_name`/`executor`/`depends_on`/`status`）；
  缺失字段时以默认值（空串 / pending）兜底，任务仍会渲染，但元信息不完整
- 无法读取的任务文件（编码错误等）会在 stderr 输出警告并跳过，不影响其他任务渲染
- 依赖拓扑用 SVG 内联绘制，不依赖任何 CDN 或外部库；检测到依赖环会在 stderr 报警
