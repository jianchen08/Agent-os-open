# 管道配置

> 返回 [开发指南索引](README.md)。Agent 配置见 [agent-configuration.md](agent-configuration.md)。

## 1. 现状

`config/pipelines/autonomous.yaml` 是**唯一现役管道**：所有 Agent 共用它，差异全部由 Agent 配置（system_prompt / tool_ids / model_tier）体现。结构总览与修改须知见 `config/pipelines/README.md`。管道在内核启动期编译（`when` 预编译 AST、引用静态解析、重名冲突启动即 panic），运行时零解析——**改完必须重启内核**。

## 2. 配置结构

```yaml
name: autonomous

loop_bodies:
  - id: init          # 前处理：workspace/environment 解析，单次执行
    steps: [ ... ]

  - id: main          # agent 自主循环：llm_call ↔ tool_execute
    while: "True"     # 恒真循环；退出靠 step 路由 end + stop_check 兜底
    steps:
      - id: prepare   # input 插件链（context_build → tool_schema → ... → prompt_build → 守卫链）
        steps: [ pipeline_context_build, pipeline_tool_schema, ..., pipeline_prompt_build ]
        context:                    # 自由 KV，merge 进 state 供插件读取（支持 {{state.x}} 模板）
          agent_id: "{{state.agent_id}}"
          model_tier: "{{state.model_tier}}"
      - id: core
        steps:
          - "{{state.core_plugin}}"   # 动态插件：由路由 set 切换 llm_core / tool_core
          - pipeline_spill_guard
        context: { agent_id: "{{state.agent_id}}", temperature: 0.7 }
      - id: post      # output 插件链 + 出口路由
        steps: [ pipeline_track, pipeline_task_reminder, pipeline_stop_check, ... ]
        next: [ ... ]                  # 出口转移 DSL，见 §3

  - id: exit          # 后处理：workspace 收尾 + 环境释放；run_on_error 保证提前终止也执行
    run_on_error: true
    steps: [ ... ]
```

**step 引用三级命中**（steps 列表项解析顺序）：
① 当前管道内的 step id（组合节点，递归展开）→ ② 公共 step 库 `config/steps/*.yaml` → ③ 插件 id（manifest 里的 id，如 `pipeline_context_build`；注意引用的是**插件 id 不是工具名**）。

## 3. G10 路由 DSL（2026-08-15 冻结）

条件永远 `when`、目标永远 `then`、附带写入用 `set`；写在节点/循环体的 `next:` 列表，自上而下首中即走，缺省 when = True：

```yaml
next:
  - when: "raw_tool_calls != [] and raw_tool_calls != None"
    then: loop            # 目标：end / loop / step id（step 级）/ 循环体 id
    set: { core_type: tool_execute, core_plugin: pipeline_tool_core }
  - when: "core_type == 'tool_execute'"
    then: loop
    set: { core_type: llm_call, core_plugin: pipeline_llm_core }
  - then: end             # 兜底
```

`while:` 控制循环体条件；转移优先级：step 级路由设置的 `state.next_phase` > 循环体 `next` > 默认顺序进入下一循环体。旧 DSL 形态（loop_config/routes/exit_routes 等）加载即报错。

## 4. per-plugin inputs

给某个管道插件传参的两条通道（走 config，不进 state、不落 trace）：
- 管道 yaml 的 step `context:`（如 `temperature: 0.7`）。
- agent yaml 的 `plugins.enabled.<plugin_id>`（如 `task_reminder: {max_reminders: 3, cooldown_seconds: 180}`）。

插件侧经 `PluginContext.config` / `plugin.get_config()` 读取（native 侧 `ectx.ctx.config_value()`）。

## 5. 修改流程与验证

1. 改 `config/pipelines/autonomous.yaml`（或前端设置页"管道"可视化编辑器，写同一文件）。
2. 重启内核（启动期编译 + 五类命名冲突检测：body/step id 重复、与插件 id 冲突、Phase 目标不存在）。
3. 跑回归：`pytest tests/test_tool_block_not_end_pipeline.py`（工具块不终结管道的核心行为闸）。

## 6. 规划中（尚未落地，勿按此编写集成）

以下能力定稿于 `docs/working/管道配置输入契约与动态管道能力设计_20260824.md`，接口未实现：
- 管道顶层 `inputs:` 输入契约声明（source: user/task/trigger/tool/init）。
- 蓝图/实例模型：`pipeline_run.execute(name, inputs)` 出生新管道实例、`chat.send_message(pipeline_id, message)` 续跑；`save+execute` 文件即接口（`config/pipelines/` 为内核保留路径，普通文件工具不可写）。
- 现实出生方式：`chat.send_message` 带 `create: true`（或空 pipeline_id）由引擎生成新 pipeline_id；`task.id = pipeline_id` 单一真值，任务状态由 task_reminder 等任务域插件裁决，内核不回写。
