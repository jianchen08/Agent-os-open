# Agent 配置

> 返回 [开发指南索引](README.md)。管道配置见 [pipeline-configuration.md](pipeline-configuration.md)。

## 1. 文件位置与层级

```
config/agents/
├── main/agentos.yaml                 # L1 主 Agent（唯一 main）
├── orchestrator/*.yaml               # L2 编排（7 个）
├── executor/                         # L3 执行（general_agent + code/ environment/ generation/ 分组）
├── system/*.yaml                     # evaluator / function_verifier / review
└── task/container_verification_agent.yaml
```

定位规则：`config/agents/` 下递归找 `<agent_id>.yaml`，文件名优先、`config_id` 回退。层级（L1→L2→L3）委托深度上限 3 层，由管道的 level_guard 按 `level` 字段拦截越级。

## 2. 字段参考（`config/agents/main/agentos.yaml` 为全字段样板）

| 分组 | 字段 |
|---|---|
| 身份 | `config_id` `name` `display_name` `description` `agent_type`（main/orchestrator/executor/system）`category` `level`（L1/L2/L3）`model_tier` `model_name`（executor 可指定如 `deepseek-v4-flash`）`version` `is_active` `status` `tags` `metadata` |
| 提示词 | `system_prompt`（支持 `{{path:...}}` 文件注入、`{{project_root}}` 占位）、`static_vars.items`（静态注入，`type: reference` 或 `{{path:...}}`）、`dynamic_vars.items`（每次执行求值，如 `{{timestamp:...}}`）、`prompt_structure`（include_* 开关 + `layer_order` 提示词分层顺序） |
| 工具面 | **`tool_ids`**（LLM 可见工具白名单，核心字段） |
| 行为约束 | `hard_constraints[]`（硬约束，进提示词）`soft_constraints[]` |
| 运行限额 | `max_iterations`（-1 不限，post 链 stop_check 强制兜底默认 20）`max_reminders` `timeout_seconds` |
| 插件参数 | `plugins.enabled.<plugin_id>`（per-plugin inputs，如 `task_reminder: {max_reminders: 3, cooldown_seconds: 180}`）`plugins.disabled[]` |
| IO 契约 | `input_schema`（用户消息结构）`output_schema` |
| executor 特有 | `deliverables[]`（产出物声明：`output_path: '{{workspace}}/reports/{{task_id}}_report.md'` 等）`recommended_metrics`（默认评估指标如 `file_check`） |
| orchestrator 特有 | `team`（固定外包的 L3 列表） |

## 3. 消费链（谁读这份 yaml）

- **内核只读一个键**：`tool_ids`（`kernel/crates/config/src/agent_loader.rs` 的 `resolve_agent_tool_ids` 窄接口，mtime 缓存热更新）。内核在构建 LLM 请求时按它过滤工具 schema 注入 `state["tool_schemas"]`。
- **全量配置归 context_build 插件**：管道 prepare 步的 `pipeline_context_build` 按 `state.agent_id` 自行加载 yaml（`plugins/shared/pipeline/input/context_build/plugin.py`），注入 `context.system_prompt`（优先级：state 已有 > agent yaml > 插件默认）、`tool_ids`、`context.agent_name`、`context.agent_level` 等。
- **agent_id 全链传导**：会话创建时写入 initial_state（默认 `agentos`），切换会话 Agent 即换 `agent_id`，后续每轮 prepare/core/post 都按它取配置。`execution_context`（workspace/隔离）同样随 initial_state 与任务参数透传。

## 4. 修改途径与生效条件

| 途径 | 覆盖 | 生效 |
|---|---|---|
| 前端 `/agents` 页（agent_manager 插件） | 12 个常用字段（config_id/name/display_name/description/agent_type/level/model_tier/system_prompt/tool_ids/max_iterations/timeout_seconds/tags），带 etag 并发保护 + .bak 备份 + 语法校验 | 立即（写的就是 yaml 文件） |
| 直接改 yaml 文件 | 全部字段（static_vars/deliverables/plugins.enabled 等表单没有的） | mtime 缓存，下一个新任务/会话生效，无需重启 |

新增 `tool_ids` 条目时要确认对应插件已启用（见[总览三层过滤链](plugin-development.md#6-llm-能看到哪些工具三层过滤链)），否则 LLM 面不会有它。

## 5. 新增一个 Agent 的步骤

1. 在 `config/agents/` 对应层级目录建 `<agent_id>.yaml`（从同层现有 agent 复制改）。
2. 必改：`config_id` / `name` / `level` / `system_prompt` 骨架 / `tool_ids`（只给该 agent 需要的工具）。
3. 需要产出物约束加 `deliverables`；需要固定下游加 `team`。
4. 验证：新会话选该 agent 发消息，观察 `context.agent_name` 与工具面是否符合预期（执行详情可用 `read_execution_detail` 工具分层查看）。
