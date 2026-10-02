# 2026-10-02 管道工具参数插件整合与澄清改名（tool_args_inject / agent_config_load）

## 背景

2026-10-02 用户对管道插件粒度三轮质疑（tool_schema/param_inject 是否合并、
context_build/prompt_build 是否重复、tool_cache 读写是否合一），评估读码后拍板：
合并质疑不成立、维持拆分，但暴露出两处真实问题，本 ADR 处置：

1. **`pipeline_injected_param_validator` 生产空转且知识双重漂移**：
   - 它读 `state["_tool_definitions"]`，该键全仓无写入方（内核不写；
     `tool_schema_validator` 走 `tool_registry` 服务、state 仅测试夹具回退），
     校验从未对真实工具声明执行过；
   - 其 `_KNOWN_INJECT_SOURCES` 硬编码表是"注入方注入什么"的跨插件手工副本：
     缺 param_inject 实际注入的 workspace / project_root / isolation_level /
     authorized_zones / authorized_read_zones / parent_ws_meta 六键；task_id
     误归引擎（实为 param_inject 从 state["task.id"] 注入）；
     `ToolCore._SERVICE_INJECT_MAP` 等 Python 时代条目在现役 Rust tool_core
     已不存在（零命中）。
   - 真实工具声明中的 `tool_record_id`（task / triggers_ext）、
     `_retriever` / `parent_record_id`（search，声明后自身代码亦未消费）
     全仓无注入方，属悬空声明——恰是该校验应捕捉的契约断裂，但它跑不起来。
2. **命名与职责错位**：`param_inject` 实际职责是"工具调用参数归一/修复/注入"
   （名字听起来像和 tool_schema 一伙）；`context_build` 实际职责是
   "agent 配置装载"（名字听起来像在拼装上下文，多次引发与 prompt_build
   重复的误判）。

## 决策

1. **删除 `pipeline_injected_param_validator` 插件**；首轮声明自检并入
   `pipeline_param_inject`（注入方自持注入知识，消掉跨插件硬编码副本）：
   - 触发：管道首轮，guard 键 `tool.injected_params_checked`（原
     `injected_param_check_done` 无外部消费方，内部键随整合一并更名）；
     注册表与 state 回退皆空时不置标记、下轮重试（语义继承原插件）。
   - 输入：`ctx.get_service("tool_registry").list_all()` 取权威定义，取不到
     回退 `state["_tool_definitions"]`（测试夹具兼容，与 tool_schema_validator
     同款模式）。
   - 注入知识 = 插件自有注入键集合（`_NATIVE_INJECT_KEYS`，与 `_inject_*`
     注入方法同文件维护）∪ config `default_params` 键 ∪
     `input_schema.properties` 豁免；命中不了的声明记 WARNING 不阻断
     （防御性语义继承）。
2. **`pipeline_param_inject` → `pipeline_tool_args_inject`**：目录 / manifest
   id+name / 类名 / `name` 属性 / 日志标签 / `invoke_entry` / pyproject 包名
   同步改。state 键 `tool.params_injected` 不变（名实相符，无 churn 必要）。
3. **`pipeline_context_build` → `pipeline_agent_config_load`**：同步范围同上。
4. 同步引用面：`config/pipelines/autonomous.yaml`、`roleplay.yaml`；kernel
   engine 测试夹具与 `capability_router.rs` 注释；前端
   `PipelineSettingsPage.test.tsx` 夹具；`tests/test_autonomous_context_build_wiring.py`
   （改名 `test_autonomous_agent_config_load_wiring.py`）；相关插件 manifest
   描述中的插件名引用；活文档 AGENTS.md / docs/ARCHITECTURE.md。

## Alternatives Considered

- **validator 移出管道到 G2 装载期校验**：被否。G2 校验的是 manifest 语法与
  结构，注入来源覆盖是运行期语义（依赖 param_inject 的 config
  `default_params` 与插件装载态）；且装载期没有 per-pipeline 的一次性 guard
  键机制，"只跑一次"语义需要另造。
- **保留 validator 独立插件、仅修正知识表**：被否。表的知识本质是"注入方
  注入什么"的复制品，修表不消除漂移机制（复制必漂移，本次正是要消掉它）；
  且其读死键（`_tool_definitions` 无人写）的生产空转缺陷仍在。
- **连带清理悬空声明（tool_record_id / _retriever / parent_record_id）与
  更名 state 键 `tool.params_injected`**：被否。悬空声明处置权在工具插件
  自身（并入后的自检 WARNING 即为其检测机制），本次不越界改工具契约；
  state 键名实相符，更名属无关 churn。

## 影响

- 契约变更：插件 id 一删两改，管道配置同批（同仓同 commit，无半新半旧
  窗口）；热发现 watcher 自动注销/重注册，无需重启内核。
- 自检由生产空转变为实际执行：接入 tool_registry 后对现网声明生效，预期对
  tool_record_id / _retriever / parent_record_id 悬空声明出 WARNING
  （真阳性，处置归工具插件，本 ADR 不处理）。
- state 键：`injected_param_check_done` → `tool.injected_params_checked`；
  `tool.params_injected` 及其余键不变。
- 装机版：管道插件随内核包分发，改名随下次应用升级整体消费；dev 侧无动作。

## 归档

- 讨论记录：2026-10-02 会话（三插件合并质疑 → 读码评估 → 用户两轮反驳修正
  → 拍板"维持拆分 + 本 ADR 两项处置"）。
- 关联：2026-09-06 T7 裁定（param_inject 的 JSON 修复经 llm.repair_json
  能力面收敛，不跨插件 import）；2026-09-28 D10（mode_material_inject 自
  context_build 抽出——该决策仅针对模式关注点，不构成"配置装载必须保持
  瘦"的一般论据，本 ADR 论证未引用它）。
