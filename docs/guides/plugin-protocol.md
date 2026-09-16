# 插件协议规则（Plugin Protocol）

> 返回 [开发指南索引](README.md)。协议权威：`plugin.json` 契约、包结构、注册发现、生命周期的一切**横切规则**（供注入遵守）。
> 模式私有知识（面板/编排/身份/规则物料怎么写）权威载体在模式包内物料，本篇只写协议与结构约束。
> 已落地不标；（规划中 P2/P3）= 已定稿未建成，不得按其编写集成。排障对照见 [troubleshooting.md](troubleshooting.md)。

## 一、协议公理

1. **一切皆插件**：内核只是执行基座，不含业务能力——LLM/工具/记忆/评估/审批/通道/主题/Agent 配置加载皆插件承载；改业务行为 = 加/改插件或配置，不动内核。
2. **插件 = 一个目录 + 一个 `plugin.json`**（也支持 `plugin.yaml`）+ 实现代码。manifest `deny_unknown_fields`：声明未知字段加载即拒（fail-closed）。
3. **目录即插件**：发现算法只把**直接含 plugin.json 的目录**当插件；无 manifest 的子目录 = 父插件经 import 引用的子模块，补 manifest 反而被误当新插件发现。
4. **插件无状态**：不持久化、不直接写存储；返回 Patch/结果由引擎决定是否应用。
5. **同 id 用户根覆盖内置根**（双根：内置 `plugins/shared/` 只读 + 用户根 `AGENTOS_USER_PLUGINS_DIR` 或 OS 标准目录可写）——不修改仓库即可替换/魔改内置插件。

## 二、包结构规则

### 2.1 标准布局

sidecar（Python）：`plugin.json` + `server.py`（MCP 适配层）+ 业务 `.py`（可选拆分）+ `test_*.py`（就地放）+ `pyproject.toml` + `uv.lock` + `.venv`（uv sync 生成，内核要求）。
native（Rust）：`plugin.json`（`host_type: in_process`）+ `Cargo.toml`（`crate-type = ["cdylib"]`）+ `src/lib.rs` + cdylib 产物（放插件目录根，与 `entry` 同名）。

### 2.2 模式包四层结构（自包含能力包）

```
mode_X/                     # 出厂种子 plugins/shared/modes/ → 播种 → <USER_ROOT>/plugins/modes/
├── plugin.json             # services(mode.describe/get_profile/http.handle) + contributes.pages
│                           # + http_endpoints(页面路由) + requires（统一依赖声明）
├── profile.yaml            # 模式档（编排不进 profile——在 pipelines/）
├── server.py               # 服务面
├── webview/*_panel.html    # 面板（自包含单文件，CSP 内联约束）
├── agents/*.yaml           # 身份（纯身份基座）
├── pipelines/*.yaml        # 编排（可选 0..N）
├── rules/*.md              # 口径（A 面，可进化）
├── policies/*              # 制度（B 面，冻结）
└── tests/
```

四层能力随包到位：面板（webview）/服务（services）/编排（pipelines/）/身份+规则（agents/ rules/）。前端零改动，禁用即同源消失。

### 2.3 约定即注册（规划中 P2）

模式包约定子目录由装载期扫描自动注册进包命名空间，G2 逐文件 schema 校验 fail-closed——**加 agent/编排 = 放一个文件，零 manifest 编辑**：

| 约定目录 | 注册为 | 校验 |
|---|---|---|
| `agents/*.yaml` | agent 键 `mode_X/<文件名>` | agent schema |
| `pipelines/*.yaml` | 编排键 `mode_X/<文件名>`（文件头 `task_kinds` 参与路由） | 管道编译器 schema |
| `webview/` | 面板页（http_endpoints 路由供给） | 供给自检 |
| `profile.yaml` / `rules/` / `policies/` | 模式档/口径/制度 | 各自 schema |

通用插件（tool/system/pipeline）的能力仍走 manifest `capabilities` 声明——与模式包约定扫描是两条注册面，勿混淆。

## 三、注册与发现规则

### 3.1 热发现全链路

新建插件目录、修改 `plugin.json`、改插件 Python 代码均由 watcher 自动处理（300ms 防抖 + 60s 轮询兜底）：发现 → G2 校验 → 注册/重注册/respawn，秒级生效，无需 re-enable 或重启；声明与实现不一致被 G2 漂移校验**拒注册**。仅 native cdylib 集合变更走 G8 自动重启（排空 + 自拉活；同 id 换产物保守重启）。禁用中的插件改完不注册——先启用，这是"禁用"语义本身。

### 3.2 三层工具过滤链

1. **启用档案**：`manifest.enabled` > `config/plugins/default_profile.yaml` > 默认 true；禁用插件整个不进注册表。watcher 每轮从盘上重读 profile（运行期改即生效）。
2. **能力注册**：`capabilities.tools[]` → ToolDescriptor 进 CapabilityRegistry；external MCP 工具缺 `input_schema` 拒注册（内置工具缺则 `{}` 补注册 + warn）。
3. **tool_ids 白名单**：LLM 实际可见 = 注册表 ∩ 当前 agent `tool_ids`；解析不出 tool_ids = **空工具面**（禁止静默全量），仅框架强制工具 `spill_retrieve` 兜底注入。

新工具要让 LLM 用到，三处都要通：插件启用 → 声明合法 → 加进目标 agent `tool_ids`。

## 四、manifest 契约规则

### 4.1 必填字段

`id`（全局唯一 snake_case）/ `name` / `version`（semver）/ `plugin_type`（`pipeline`/`tool`/`system`/`composite`）/ `host_type`（`sidecar` 默认 | `in_process`）/ `capabilities` / `language` / `entry`（composite 可空）。
条件必填：`pipeline_role` + `invoke_entry`（plugin_type=pipeline）；`native`（in_process）。

### 4.2 可选字段速览

| 字段 | 规则 |
|---|---|
| `requires_services` | 插件间耦合唯一轴（现行）：条目 = 能力角色名（`ns` / `ns.method`），不点名插件 id；boot 期依赖闸，无人提供该角色内核拒启 |
| `permissions` | 文件/网络/环境/系统调用声明，默认全空 |
| `priority` | 同阶段执行顺序，越小越靠前，默认 100 |
| `config_files` | 配置显式映射 `{id, path, label}`；见 §4.4 |
| `ui_schema` | 前端 schema 驱动 widgets（`type`/`space`（chat/workspace/floating/dock/fullscreen；`scene` 已废弃）/`trigger`/`props`）；见 §4.4 |
| `http_endpoints` | 每项 `{route_id, method, path, auth, handler_capability, timeout_ms}`；path 必须落 `/ext/{plugin_id}/**`，经 capability RPC 调插件 `http.handle` |
| `contributes` | 前端贡献点（pages/viewsContainers/widgets/menus/commands/settingsPanels…）；内核仅在 `/api/v1/schema` 透传，前端 ContributionRegistry 消费——模式面板 tab / 聊天 input-action 声明即现 |
| `mcp` | 接入外部 MCP 服务（`transport: streamable_http` 用 endpoint.url/headers/auth；`stdio` 用 command/args/env）；`${VAR}` 占位构造时解析；长等待业务必须显式 `request_timeout_secs`（默认 300s 掐断） |
| `lifecycle` | 生命周期覆盖（如 `idle_timeout_secs: 0` = 永不卸载，交互类必设） |
| `host_group` | `"light"` = 准入轻量合宿组（作者担保无阻塞/无 C 扩展/无重依赖）；缺省独占宿主 |
| `granted_capabilities` | 反向 capability 调用白名单；空 = 默认全授予（存量兼容），非空即白名单制越权单点拒绝 |
| `enabled` | `false` = 已安装不进注册表；缺省由 default_profile.yaml 决定 |
| `activation` | `eager`/`lazy`（默认）/`manual` |
| `persistent_fields` / `export_fields` | state 累计键持久化投影 / 出口白名单（支持 `前缀.*`；未声明且不在内核基线 = 不出口） |
| `requires_content` | 需要预加载的最近消息条数（blobs 懒加载） |

**禁声明项**（声明即加载失败）：`error_policy`（已从内核契约整体移除，错误由引擎自动处理：瞬态崩溃 respawn+重试一次、工具失败回喂 LLM 自修、非瞬态上抛编排）；`dependencies` / `capabilities_required`（不是字段，Python 依赖走 pyproject.toml）；`config_refs`（已被 `config_files` 取代）；`route_signals` 新声明（遗留位，执行面零消费——路由由管道 G10 DSL 驱动）。

### 4.3 capabilities 四面

- `tools[]`：声明即注册进 LLM 工具面。必须带全 `input_schema` + `output_schema` + `render`（工具契约 fail-closed：tool_core 执行后按 output_schema 校验，前端按 render 意图路由渲染）；`category` 省略默认 `system`。
- `services[]`：内部服务方法元数据，经 capability 调用（调用方声明 `requires_services`），**不进 LLM 面**。
- `lifecycle_hooks`：`on_load`/`on_unload`/`on_pipeline_start`/`on_pipeline_end`/`on_error`/`domain_event`。
- `streaming`：发流式事件的插件必须声明 `{events, part_types, persist}`，未声明网关拒绝——契约见 [streaming-protocol.md](streaming-protocol.md)。

### 4.4 配置与 UI 面

- `config_files`：需要哪个配置文件就显式映射哪条，未声明（或空）= 收空配置（fail-closed）；`path` 相对 `config/` 根且必须落 `config/` 子树内；经 `/api/v1/plugins/{id}/config/{file_id}` 读写，mtime 热更新；追加 `"settings": false` = 注入专用不出口到 schema（UI 由插件自声明承载，避免双入口）。内核保留文件（plugin_allowlist/plugin_roots/auth/pipelines/steps）不可映射。
- `ui_schema` / `contributes` / `http_endpoints` 是三个声明面：新增插件时前端自动长出对应界面，禁止为单插件改 `frontend/src`（前端冻结铁律见 [ai-coding-spec.md](ai-coding-spec.md)）。

### 4.5 依赖与引用（目标语义）

- **统一一条 requires = 服务角色（规划中 P3）**：`requires: ["memory", "order:下单"]`——复用既有 `requires_services` 能力角色语义（消费方依赖契约而非实现），角色由注册表解析到提供者插件；解析不到 = boot 期依赖闸拒绝。工具级精确选择留在配置面（tool_ids / material_scope），装载解析时逐名 fail-closed——精确性在解析处，不在声明处，杜绝依赖清单爆炸。
- **跨包引用一律全限定 `plugin_id:resource`**（规划中 P3；tool_ids 裸名随迁包一刀切退役——裸名遮蔽 bug 家族语法层面根除）；**包内引用裸 id + 相对包根路径**；系统侧留守物（config/rules）走约定路径；运行中任务 state 旧 ids 只读可解析，新写面一律新语法。

### 4.6 宿主双轨

`sidecar`（独立进程，MCP over stdio，默认——进程隔离）/ `in_process`（Rust cdylib 进程内零 IPC——高频热路径晋升轨）。双轨对所有插件类型开放；**能力以 capability 协议唯一定义一次，两轨差异只允许存在于 transport 适配**；晋升管线"边车 → 基准 → in_process"；wasm 轨已关闭。native 细节见 [plugin-native-rust.md](plugin-native-rust.md)。

## 五、sidecar（Python）运行时规则

- **uv venv 单轨**：entry 首词为裸 `python`/`python3` 时，内核强制使用**插件目录内**解释器；`pyproject.toml` 与 `.venv` 缺一启动即报（`PYPROJECT_MISSING` / `VENV_INTERPRETER_MISSING`），不回退 PATH 裸 python。初始化：`uv sync --project <插件目录>`。
- **依赖声明**：SDK 不在任何 registry，必须本地源映射——`[tool.uv.sources] agentos-plugin-sdk = { path = "<相对 sdk>", editable = true }`（tools/ 下三级、pipeline/ 下四级）。
- **stdout 被 JSON-RPC 独占**：插件日志一律走 stderr（SDK logger 已配好），`print()` 即破坏协议。
- **生命周期**：懒启动（首次调用才 spawn）→ 握手按 `config_files` 注入 → `notifications/on_load` → 空闲 GC（默认 300s，`lifecycle.idle_timeout_secs` 覆盖）→ 崩溃自动 respawn 并重试一次 → 目录 mtime 变化热重载。内核透传 `LOG_LEVEL`/`LOG_JSON`/`LOG_FORMAT`。
- **管道插件三层**：`plugin_type: "pipeline"` + `pipeline_role` + `invoke_entry`（必填，与 server.py 注册的工具名完全一致）。实现两层：`plugin.py` 业务类（基类在 `plugins/shared/pipeline/_base/`：`IInputPlugin`/`IOutputPlugin` 返回 `PluginResult{state_updates, skip_remaining, error}`；`ICorePlugin` 返回 dict 直接合并 state）+ `server.py` MCP 适配层。
- **出口裁决不走返回值**：插件经 `state_updates` 写入路由 DSL 条件依赖的字段参与裁决（如 task_reminder 写任务状态供评估闸门判定）。反例：期待 `OutputResult.route_signal` 参与路由（遗留字段，执行链不消费）。
- **测试**：就地 `test_*.py` + 分层 marker（`--strict-markers` 强制）；`importlib.util.spec_from_file_location` 显式路径加载被测模块；`PluginContext(state=..., config=...)` 直测 execute；mock 只打外部依赖，关键路径走真实依赖。

## 六、种子生命周期与用户仓 git（模式包）

- **播种**：出厂种子 `plugins/shared/modes/` 整目录拷贝到 `<USER_ROOT>/plugins/modes/`；用户从此拥有并直接改这份（不受工作区还原影响）。
- **账本**：`<USER_ROOT>/plugins/modes/.seeds.json` 每模式记 `{seeded_version, files: sha256}`。
- **启动对账**（内核启动早期、插件扫描前）：出厂 version ≤ 账本 → no-op（幂等）；未定制（哈希==账本）→ 静默升级；已定制 → 保留 + 升级可用通知；手工副本 → 补账不替换；非 semver → 保守不动。
- **恢复出厂**：删用户副本（同 id 用户赢回落 factory）+ 清账本条目。G2 对用户副本同样 fail-closed。
- **用户目录 git 化（规划中 P3）**：`plugins/` + `config/` 层入用户仓，**`.env` 密钥与 `data/` 排除**；晋升 = 用户仓 commit，审计/回滚/合并 git 原生；热重载直达副本即刻生效。

## 七、SDK 速查

```python
from agentos_plugin_sdk import AgentOSPlugin, tool, collect_tools, CapabilityHandle
plugin = AgentOSPlugin("my_plugin")

@plugin.tool(name="search", schema={...}, description="搜索")       # 或 plugin.register_tool(...)
async def search(query: str) -> dict: return {"results": [...]}

@plugin.on_load
async def on_load(params: dict) -> None: ...                        # on_unload 同理；其余 on_lifecycle(event, fn)

if __name__ == "__main__": plugin.run()   # stdin JSON-RPC / stdout 响应；KernelChannel 反向调用同流复用
```

声明了 `requires_services` 的插件握手后经 `get_capability("service-registry")` / capability handle 反向调用提供方（详见 SDK `capability.py`）。

## 八、示例插件速查

| 学什么 | 看哪里（真实插件即样板，不设玩具教程） |
|---|---|
| 最小工具插件 | `plugins/shared/tools/simple/` |
| services + http_endpoints + config_files | `plugins/shared/system/llm/` |
| requires_services + 审批闭环 | `plugins/shared/system/approval/` |
| 管道 input 插件 / agent 配置自持加载 | `plugins/shared/pipeline/input/context_build/` |
| 管道 output 插件 / 评估闸门 | `plugins/shared/pipeline/output/task_reminder/` |
| native 插件（cdylib） | `plugins/shared/pipeline/output/sensitive_checker/`、`plugins/shared/pipeline/core/tool_core/` |
| external MCP（HTTP / stdio / 零声明观测导入） | `plugins/shared/tools/external_mcp/`（接入规则见 [plugin-external-mcp.md](plugin-external-mcp.md)） |
| 插件主题/皮肤声明 | `plugins/shared/system/dsh_adapter/`（主题规则见 [theme.md](theme.md)） |
| 模式包结构 | `plugins/shared/modes/`（四模式种子；约定扫描注册规划中 P2） |
