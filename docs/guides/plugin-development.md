# 插件开发总览：类型 · 宿主 · 目录 · 注册

> 返回 [开发指南索引](README.md)。本篇讲插件体系全景与接入机制；
> 具体写法见 [sidecar（Python）](plugin-sidecar-python.md) / [native（Rust）](plugin-native-rust.md) /
> [外部 MCP](plugin-external-mcp.md) 分篇。manifest 全字段见 [docs/plugin-protocol.md](../plugin-protocol.md)。

## 1. 插件类型（plugin_type）与目录

| plugin_type | 职责 | 目录 |
|---|---|---|
| `tool` | 暴露 LLM 工具（`capabilities.tools` 进 LLM 面） | `plugins/shared/tools/` |
| `system` | 内核级服务：记忆/审批/评估/LLM/页面插件等，通常 tools + services 混合，可产路由信号 | `plugins/shared/system/` |
| `pipeline` | 管道步骤（必须声明 `invoke_entry`，可选 `pipeline_role: input/core/output`） | `plugins/shared/pipeline/{input,core,output}/` |
| `composite` | 组合插件（entry 可空，聚合子插件） | 少用 |

管道三角色职责：**input** = 预处理（校验/上下文注入/权限），**core** = LLM 调用或工具执行（返回 dict 直接合并 state），**output** = 后处理与路由信号（返回 OutputResult）。插件间经管道 `state` 字典通信，不直接互调。

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
3. **配置即快照**：`enabled_plugin_ids` 是启动期快照；改 `plugin.json` 后必须 re-enable（§7）。

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

内置根 `plugins/shared/` + 用户根（环境变量 `AGENTOS_USER_PLUGINS_DIR` 或 OS 标准目录）。同 id 用户根覆盖内置。发现算法只把**直接含 plugin.json 的目录**当插件；无 manifest 的子目录只是父插件的 Python 模块。新建插件目录后内核 watcher 5-8s 热发现 manifest。

## 6. LLM 能看到哪些工具：三层过滤链

1. **启用快照**：内核启动时按 `manifest.enabled > config/plugins/default_profile.yaml > 默认 true` 算出 `enabled_plugin_ids`；禁用的插件整个不进注册表（工具/路由信号/HTTP 路由全不暴露）。`default_profile.yaml` 形如：
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
| 新建插件目录 / 修改 plugin.json | 等 5-8s 热发现，然后 **re-enable**（前端插件设置页开关，或 `PUT /api/v1/plugins/{id}/enabled`）——会触发 G2 复核（spawn sidecar 校验声明与实现一致）并重注册能力 |
| 改插件 Python 代码 | 空闲 TTL 后热重载（kill + respawn）；不确定就重启内核 |
| 改 agent yaml | mtime 缓存热生效，下一个新任务/会话生效 |
| 改管道 yaml（autonomous.yaml / config/steps/） | 无需重启：下次执行前 mtime 热重载（1s TTL；坏配置保留旧 + warn） |
| 前端新增 ui_schema/表单 | 前端刷新页面 |

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
