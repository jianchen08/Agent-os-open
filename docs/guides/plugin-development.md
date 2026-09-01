# 插件开发总览：类型 · 宿主 · 目录 · 注册

> 返回 [开发指南索引](README.md)。本篇讲插件体系全景与接入机制；
> 具体写法见 [sidecar（Python）](plugin-sidecar-python.md) / [native（Rust）](plugin-native-rust.md) /
> [外部 MCP](plugin-external-mcp.md) 分篇。manifest 全字段见 [docs/guides/plugin-protocol.md](plugin-protocol.md)。
> 想了解内核契约与决策的来龙去脉（traits.rs / manifest 演进 / 契约间依赖）：[契约文件解析教程](contract-files-tutorial.md)。

**一切皆插件**：内核只是执行基座（管道解释执行、能力注册表、插件发现/装载/调用、存储、会话/租户/HTTP 基础设施），不含业务能力——LLM 调用、记忆、评估、审批、触发器、通道、主题、乃至 Agent 配置的加载本身，都以插件承载（能力→插件对照表与"为什么一切皆插件"六条动因见 [ARCHITECTURE.md 设计哲学](../ARCHITECTURE.md#1-一切皆插件everything-is-a-plugin)：自进化落地机制、改动半径分离、故障隔离、语言生态自由、统一契约、治理单点）。改任何业务行为 = 加/改插件或配置，不动内核。

## 1. 插件类型（plugin_type）与目录

| plugin_type | 职责 | 目录 |
|---|---|---|
| `tool` | 暴露 LLM 工具（`capabilities.tools` 进 LLM 面） | `plugins/shared/tools/` |
| `system` | 内核级服务：记忆/审批/评估/LLM/页面插件等，通常 tools + services 混合 | `plugins/shared/system/` |
| `pipeline` | 管道步骤（必须声明 `invoke_entry`，可选 `pipeline_role: input/core/output`） | `plugins/shared/pipeline/{input,core,output}/` |
| `composite` | 组合插件（entry 可空，聚合子插件） | 少用 |

管道三角色职责：**input** = 预处理（校验/上下文注入/权限），**core** = LLM 调用或工具执行（返回 dict 直接合并 state），**output** = 后处理与出口裁决（返回 OutputResult；出口转移本身写在管道 YAML 的路由 DSL，插件经 `state_updates` 写入 DSL 条件依赖的字段参与裁决）。插件间经管道 `state` 字典通信，不直接互调。

## 2. 宿主形态（host_type）

| host_type | 语言 | 机制 | 适用 |
|---|---|---|---|
| `sidecar`（默认） | Python | 独立进程，stdio 上的 MCP JSON-RPC；uv venv 单轨 | 一切常规场景、第三方贡献者、低频插件 |
| `sidecar` + `entry: "mcp:external"` | external | 不写代码，直连第三方 MCP 服务（HTTP 或本地命令） | 接入现成 MCP 工具 |
| `in_process` | Rust (cdylib) | libloading dlopen，C-ABI 取 trait 对象，进程内零 IPC | 高频插件（每轮必执行的管道步骤），从边车轨基准晋升 |

选型依据（ADR `2026-07-13-sidecar-process-model.md`、`2026-08-15-plugin-two-track-and-cordis-mechanisms.md`）：
两轨对所有插件类型开放，开发者按性能需求自选；制度化晋升管线为"边车 → 基准 → in_process"。
wasm 轨已关闭。**能力以 capability 协议唯一定义一次，两轨差异只允许存在于 transport 适配**——
同一插件从 Python 迁到 Rust 时 manifest 的 capabilities 声明不变。

## 3. 三条铁律

1. **声明即注册**：`capabilities.tools` 里声明的工具自动注册进 LLM 工具面（经 `tool_ids` 过滤，见 §6）；`capabilities.services` 是内部服务方法元数据，**不进 LLM 面**。
2. **工具契约 fail-closed**：工具必须带 `input_schema` + `output_schema` + `render`（前端渲染意图）；tool_core 执行后按 output_schema 校验，不通过即失败。
3. **改动热生效**：新建插件目录、修改 `plugin.json`、改插件 Python 代码都由内核 watcher 自动处理（发现→G2 校验→注册/重注册/respawn），无需 re-enable 或重启内核；仅 native cdylib 插件集合变更走 G8 自动重启（§7）。

## 4. 单插件标准布局

sidecar（Python）：

```
plugins/shared/tools/<name>/
├── plugin.json          # 清单（必有）
├── server.py            # MCP 适配层：AgentOSPlugin + register_tool/@plugin.tool + run()
├── <业务>.py            # 纯函数实现（可选，管道插件常拆 plugin.py 业务类）
├── test_*.py            # 单元测试（就地放插件目录）
├── pyproject.toml       # 依赖声明 + uv 源映射
├── uv.lock              # uv sync 生成
└── .venv/               # uv sync 生成（内核要求，见 sidecar 分篇）
```

native（Rust）：

```
plugins/shared/pipeline/<role>/<name>/
├── plugin.json          # host_type: in_process
├── Cargo.toml           # crate-type = ["cdylib"]
├── src/lib.rs           # 实现 + agentos_plugin_create 导出
├── <artifact>.dll       # cargo build --release 产物，放插件目录根
└── tests/ 或 src 内 #[cfg(test)]
```

## 5. 双根发现

内置根 `plugins/shared/` + 用户根（环境变量 `AGENTOS_USER_PLUGINS_DIR` 或 OS 标准目录）。同 id 用户根覆盖内置。发现算法只把**直接含 plugin.json 的目录**当插件；无 manifest 的子目录只是父插件的 Python 模块。watcher（notify 事件 300ms 防抖 + 60s 轮询兜底）热发现新插件并**自动注册**；已注册插件的 manifest 变更自动 revoke + 重注册（G2 漂移校验）。

## 6. LLM 能看到哪些工具：三层过滤链

1. **启用档案**：按 `manifest.enabled > config/plugins/default_profile.yaml > 默认 true` 判定启用；禁用的插件整个不进注册表（工具/HTTP 路由全不暴露）。watcher 每轮 sync 从盘上**重读** profile（运行期改 default_profile.yaml 或前端开关即生效）。`default_profile.yaml` 形如：
   ```yaml
   plugins:
     simple_tools:
       enabled: true
     widget_demo:
       enabled: false
   ```
2. **能力注册**：`capabilities.tools[]` 转成 ToolDescriptor 进 CapabilityRegistry。external MCP 工具缺 `input_schema` 直接拒注册（内置工具缺则 `{}` 补注册 + warn）。
3. **tool_ids 白名单**：LLM 实际可见工具 = 注册表 ∩ 当前 Agent 的 `tool_ids`（`config/agents/<...>/<agent_id>.yaml`）。解析不出 tool_ids = **空工具面**（禁止静默全量），仅框架强制工具 `spill_retrieve` 兜底注入。

所以新工具要让 LLM 用到，三处都要通：插件启用 → 声明合法 → 加进 agent 的 `tool_ids`。

## 7. 改动与生效动作对照

| 改了什么 | 需要做什么 |
|---|---|
| 新建插件目录 / 修改 plugin.json | 无需动作：watcher 自动发现 + 注册/重注册（含 G2 漂移校验），秒级生效 |
| 改插件 Python 代码 | 无需动作：invoker 检测到目录 mtime 变化即 kill + respawn，新代码生效 |
| 增删 native（cdylib）插件 | 自动：watcher 触发 G8 优雅重启（排空 + 自拉活）；同 id 替换产物文件保守起见重启内核 |
| 改 agent yaml | mtime 缓存热生效，下一个新任务/会话生效 |
| 改管道 yaml（autonomous.yaml / config/steps/） | 无需重启：下次执行前 mtime 热重载（1s TTL；坏配置保留旧 + warn）。新插件热注册后在管道里引用其 id 即可编译 |
| 前端新增 ui_schema/表单 | 前端刷新页面 |

禁用中的插件（default_profile.yaml 或前端开关置 false）改完代码/manifest 不会注册——先启用，这是"禁用"语义本身。

## 8. 命名与 State 约定

### 8.1 命名规范

| 命名对象 | 格式 | 示例 | 说明 |
|----------|------|------|------|
| 插件 id / 目录名 | `snake_case` | `memory_read`、`stop_check` | 用于 manifest `id`、目录名、管道引用（管道插件 id 惯例带 `pipeline_` 前缀，如 `pipeline_context_build`） |
| 业务类名（管道插件） | `{CamelCase}Plugin` / Core 插件 `{CamelCase}Core` | `ContextBuildPlugin`、`ToolCore` | `plugin.py` 业务实现类 |
| 工具名 | 小写 + 可选点分命名空间 | `yaml_validate`、`llm.complete_stream` | 与 manifest `capabilities.tools[].name` / SDK 注册名完全一致（含大小写） |
| 命名禁区 | 禁驼峰目录名、禁与现有插件 id 冲突 | — | 同 id 用户根会覆盖内置根，重名即静默覆盖 |

### 8.2 State 命名空间约定

插件经 `state_updates` 写状态，读取/写入遵循归属约定，避免互相踩踏：

| 角色 | 写入（示例） | 读取 |
|---|---|---|
| Input | `context.*`（system_prompt / agent_name / agent_level）、`tool_ids`、`security.*`、`execution_contexts` | 用户消息、会话元数据 |
| Core | `raw_result`、`raw_tool_calls`、`tool_results`、`core_type` | 所有 Input 写入的命名空间 |
| Output | `task.*`（经 pipeline-state 写面）、路由 `set:` 变量 | Core 写入的命名空间 |

原则：写自己的命名空间，不覆写其他插件的键；跨插件数据需求经 `requires_services` 走服务调用，不偷读他人内部键。
