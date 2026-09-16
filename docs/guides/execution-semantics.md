# 执行语义规则（Agent 身份 × 编排）

> 返回 [开发指南索引](README.md)。横切规则物料（供注入遵守）：只写跨模式公共契约。
> 模式私有 agent 家族与场景编排的权威载体 = 模式包物料 `<USER_ROOT>/plugins/modes/<mode>/{agents/,pipelines/}`，本篇不复制其内容。
> 主线依据：`docs/working/模式体系落地设计_20260915.md` §三。已落地不标；（规划中 P2/P3）= 已定稿未建成，不得按其编写集成。

## 一、Agent 身份规则

### 1. 双来源注册表

- 系统侧 `config/agents/`：**治理留守清单**——main、系统 agent、安全红线相关身份，留系统侧冻结。布局：`main/`（L1 唯一）/ `orchestrator/`（L2）/ `executor/`（L3）/ `system/` / `task/`；定位规则：递归找 `<agent_id>.yaml`，文件名优先、`config_id` 回退；委托深度上限 3 层（`level` 字段，level_guard 拦截越级）。
- 模式包 `agents/*.yaml`：约定扫描注册为键 `mode_X/<文件名>`，G2 逐文件 schema 校验 fail-closed；加模式 agent = 放一个文件，零 manifest 编辑（规划中 P2）。
- agent 解析两级：系统注册表 → 模式包注册表；两级未命中 fail-closed，禁止回退默认人格。
- 同 id 冲突按双根兜底：用户副本赢出厂种子；删副本回落 factory。
- 全字段样板见 `config/agents/main/agentos.yaml`（本篇不逐字段展开）。

### 2. agent yaml 只装身份性字段

- 准入三字段族：persona（`system_prompt`，支持 `{{path:...}}` 文件注入与 `{{project_root}}` 占位）、基线工具面（`tool_ids`）、model tier（`model_tier`/`model_name`）。
- 场景分化职责归编排：禁止为场景 fork agent；禁止在 agent yaml 写编排私有的步骤参数或路由状态。

### 3. agent_id = 初始输入的命名模板（三视角，一条主线）

- 初始输入 = 三方装配：agent 基座（身份性字段）+ task 提交（任务性字段：goal/工作空间/验收标准）+ 模式物料键（规划中 P2）。
- 派发期契约：三方**并集** ⊇ 所选编排的派生输入集（成员插件 required 字段并集，manifest 静态可推导）；缺字段门口拒派（规划中 P2）。完备性约束在初始输入整体上，不在单个 yaml。
- 运行期构成：有效上下文(agent_id, 编排) = 基座 ⊕ Σ 编排内各插件上下文贡献，按编排配置逐段设置。
- yaml 边界（视角③）：persona/基线工具面/model tier。

### 4. 消费与编辑

- **内核零 agent 配置知识**（不读 `config/agents/**`）：全量加载归 context_build 插件按 `state.agent_id` 自持，`tool_ids` 与提示词同源注入；agent_id 只是执行上下文键（随 `execution_context` 贯穿任务链），内核只透传。
- agent_manager **双面编辑**（读面聚合注册表、条目标 `system|mode:<id>` 来源：规划中 P3）：系统侧 agent → 写 `config/agents`（admin 门控；12 常用字段表单，etag 并发保护 + .bak 备份 + 语法校验）；模式包 agent → 写用户副本文件并落用户仓 commit（规划中 P3）。UI 是编辑器、git 是同一份文件的版本层，不是两个竞争写者。
- 直接改 yaml：全部字段可用；mtime 缓存，下一个新任务/会话生效，无需重启。反例：改完 yaml 期待在途任务生效——只对新任务生效。

### 5. 工具面（tool_ids）

- LLM 可见工具 = 启用档案 ∩ 能力注册 ∩ 当前 agent `tool_ids`；解析不出 tool_ids = **空工具面**（fail-closed，仅框架强制工具 `spill_retrieve` 兜底注入）。三层过滤链细则见 [plugin-protocol.md](plugin-protocol.md)。
- 新增 `tool_ids` 条目前置：对应插件已启用且声明合法，否则 LLM 面不会有它。
- 收窄规则：工具面只允许编排收窄（与基座取交集），禁止任何一层扩权（随物料档注入落地，规划中 P2）。

## 二、编排规则

### 6. 共享默认管道唯一，模式编排只增不改

- `config/pipelines/autonomous.yaml` 是系统侧**唯一默认管道**（所有 agent 共用，差异由 agent 配置体现）。启动期加载编译，运行时零解析；每次执行前 Pull 热加载（mtime 指纹 1s TTL），改完无需重启；热重载失败（坏 YAML/命名冲突/编译错误）**保留旧配置 + warn**，在途 run 按快照跑完；启动期才 fail-fast。
- 模式包 `pipelines/*.yaml` 可选 0..N（按模式内任务型分工，一般一个就够），文件头 `task_kinds` 标注参与路由（规划中 P2）。
- **共享骨架非进化对象**：模式包只增自己的编排，永不改共享骨架；模式包内编排文件是进化对象。

### 7. 三级编排解析

① 任务显式指定**编排键** → ② 会话绑定 agent 对应其编排：main 会话由附带的模式路由信息（编排清单/调哪些 agent）自行分析任务状态选择编排；绑定专属 agent 则用其所属模式包编排（规划中 P2）→ ③ 共享默认 autonomous 兜底（意图不明/未命中时）。注意：编排键是管道定义，`pipeline_id` 是运行实例 ID（实例化后才生成）。现行：② 未接线，一切任务走 ③。
- agent 绑定不参与函数选择；`default_pipeline` 设计已废，禁止在 agent yaml 声明默认管道。

### 8. 编排选择 = 模式插件职责

- 模式级分化发生在**管线之前（选编排）**，不在管线之内；步骤插件只面对已被选定的编排，行为由该编排的配置决定。
- 反例（违规）：步骤插件读 state 的模式键自行分叉行为；为差异化复制/篡改 autonomous.yaml；在编排内按模式长分支。
- 正例：百角色共用同一条角色扮演编排——换角色 = 换输入，不换函数。
- 路由依据（task_kinds 声明 + 任务状态/上下文分类逻辑）都在模式包内，路由本身是可进化物料。

### 9. 管道 = 函数

- 编排定义**函数签名**（派生输入集）；一条编排对应多种输入（同签名不同身份/字段值）；**一个输入不喂多个函数**——语义不同的场景 = 不同编排。
- 函数选择按任务意图，输入按所选函数签名装配，两步分离；agent 不是编排的枢轴。
- 编排内优先级单一方向：管道步骤配置 > agent 基座 > 插件默认。

### 10. G10 路由 DSL（冻结契约）

- 条件永远 `when`、目标永远 `then`、附带写入用 `set`；写在节点/循环体的 `next:` 列表，自上而下首中即走，缺省 `when` = True。`then` 目标：`end` / `loop` / step id / 循环体 id；`while:` 控制循环体条件；转移优先级：step 级 `state.next_phase` > 循环体 `next` > 默认顺序进入下一循环体。旧 DSL 形态（loop_config/routes/exit_routes）加载即报错。
- step 引用三级命中：当前管道内 step id → 公共 step 库 `config/steps/*.yaml` → 插件 id（引用的是插件 id，不是工具名）。

### 11. 插件传参（per-plugin inputs）

- 两条通道（走 config，不进 state、不落 trace）：管道 yaml 的 step `context:`；agent yaml 的 `plugins.enabled.<plugin_id>`。插件侧经 `PluginContext.config` / `plugin.get_config()` 读取。

### 12. 验证与规划项

- 管道行为闸：`pytest tests/test_tool_block_not_end_pipeline.py`；前端设置页"管道"可视化编辑器写同一文件。
- （规划中）管道顶层 `inputs:` 输入契约、蓝图/实例模型：定稿于 `docs/working/管道配置输入契约与动态管道能力设计_20260824.md`，接口未实现，勿按此编写集成。
