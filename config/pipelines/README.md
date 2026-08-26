# Pipeline 配置（0.2 现实）

本目录是**唯一**的管道配置源。0.1 时代的 `default.yaml` / `l1-main.yaml` /
`l2-evaluator.yaml` / `l2-subtask.yaml`（扁平 input_routes + inherit 继承格式）
已于 2026-08-21 退役删除——它们在内核 0.2 加载路径（`pipeline_loader.rs`
只读 `autonomous.yaml`）零消费方，属死配置。

## 目录

```
config/pipelines/
├── README.md          # 本文件
└── autonomous.yaml    # 唯一现役管道（G10 DSL 多循环体模型）
```

## 现役管道：autonomous.yaml

所有 Agent 共用同一条管道，差异由 Agent 配置（`config/agents/**/*.yaml` 的
system_prompt / tool_ids / model_tier）体现，管道文件不按 Agent 区分。

- **加载**：内核启动期 `pipeline_loader.rs::load_pipeline_config`
  硬编码加载 `config/pipelines/autonomous.yaml`；文件缺失时返回空配置 +
  warning（不会崩，但管道无步骤可执行）。
- **格式**：G10 统一 DSL（2026-08-15 冻结）——`loop_bodies` 顺序执行、
  条件永远 `when`、目标永远 `then`、缺省顺序推进；配置加载期编译成执行计划
  （when 预编译 AST、引用静态解析、语法错误启动即报），运行时零解析。
- **三级步骤命中**：① 当前管道 step id（组合节点递归）→ ② 公共 step 库
  （`config/steps/`）→ ③ 插件名（manifest id → 原子插件 invoker 调用）。
- **循环体语义**：
  - `init`：前处理（workspace_lifecycle 建空间 / environment_lifecycle 建隔离环境），单次执行
  - `main`：agent 自主循环（prepare → core → post），`next` 出口 DSL 决定
    回 LLM 还是执行工具；`ended` 只结束本体循环，顺序推进到 exit
  - `exit`：后处理（workspace 合并 / 环境释放），`run_on_error: true`
    保证提前终止（ended/出错）也执行收尾；挂起（suspended）不触发

## 修改与验证

- 改 `autonomous.yaml` / `config/steps/*.yaml` **无需重启内核**：每次 chat 执行前
  Pull 热加载检测配置 mtime（1s TTL 门），变化即重新加载 + 编译（server.rs
  `maybe_reload_compiled_pipeline`）。坏 YAML / 命名冲突 / 编译错误会保留旧配置
  + warn（不 panic，改坏不致 chat 不可用）；在途 run 按快照跑完不受影响。
  启动期加载与校验（含命名冲突 fail-fast）保留，作为首启校验与热重载失败兜底。
- 改动后跑：`tests/test_tool_block_not_end_pipeline.py`（锁定现役管道
  为 G10 格式、不得回退旧 input_routes / target=end 拦截路由）。
- 契约参考：`docs/working/重要设计/插件三轨一致性与Cordis机制迁移计划.md` §G10。
