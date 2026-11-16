# dsh_adapter — DSH 插件适配器

> task_dsh_plugin_adapter。让 DSH 的工具插件与视觉插件在灵汐稳定运行。
> **版本基准 0.2.1-rc.5**（npm next 稳定线，git tag `dsh-v0.2.1-rc.5` =
> `b830ec950f`，2026-11-04 自 0.1.0-rc.8 升级；工具契约面零漂移，见
> `docs/working/DSH适配器翻译规格_20261020.md` §2.2）。**DSH 源码零改动**
> （MIT 出处见各文件头；rc8 参考仓 `D:\reference_repos\deepseek-harness-rc8`
> 已降级为桥协议回归测试夹具——运行时不再依赖仓库产物，boot 走 npm
> 运行时工作区，见下文〔运行时工作区〕节）。

## 结构

```
dsh_adapter/
├── plugin.json            # 2 工具契约（均带 output_schema+render）+ contributes
├── server.py              # sidecar 入口（@plugin.tool 注册面 + 皮肤递送端点）
├── translator.py          # 清单翻译器（纯函数：DSH 包 → 灵汐注册清单）
├── bridge.py              # Node runtime 宿主（spawn/JSON-RPC/超时/惰性boot/重启）
└── runtime/
    └── dsh-rpc-bridge.mjs # 通道 A fork：boot DSH cordis context，经 stdio 暴露工具
```

## 适配面：插件带什么就适配什么

**后端工具**（通道 A）。`runtime/dsh-rpc-bridge.mjs` 从**运行时工作区**
（用户空间 npm 项目，见下节）解析 `@deepseek-ai/*` 基础包与已装插件包，
boot 按 `bootPlan` 裁剪的最小 cordis context：

```
SystemPrompt → ToolRuntime(dsh-tools) → LocalFileSystem → SubprocessLocal
→ SessionProjection → WorkingDirectory(defaultDirectory=cwd)   ← 0.2.1 链新增（tool-fs inject 依赖）
→ ToolFs(read/write/edit/…) → ToolFsSearch(glob/grep, sampleOverCapGlobResults=true)
→ bootPlan.packages（已启用插件包逐个 import+apply）
```

**boot 裁剪轴（AC-R4）**：`initialize {bootPlan}` 下发
`{chain, webServer, hooks, packages}`——`chain:false`（空工作区/无启用包）
不 import 任何 DSH 包（含 cordis），initialize 返回空工具清单；webServer/
钩子轴为 P1 声明位（信号面在管理端点如实报告）。

协议（newline-delimited JSON-RPC over stdio，stderr 走日志）：

| 方法 | 说明 |
|---|---|
| `initialize {cwd, bootPlan}` | boot 裁剪 context + 返回工具契约清单（name/description/input/output schema） |
| `tool/call {name, args, timeoutMs}` | 直接执行（构造最小 ToolRunContext），返回灵汐信封 `{success, data, error, duration_ms}` |
| `shutdown` | dispose + exit 0 |

### 运行时工作区（用户空间，DSH 插件包=用户数据）

- **落位解析序**：显式配置 > `AGENTOS_DSH_RUNTIME_ROOT` > 用户空间默认
  （`user_root()/dsh_runtime`；dev=`<repo>/user_root/dsh_runtime/`，
  装机=`%APPDATA%\agentos\dsh_runtime\`，与内核 `core::user_space` 同源）。
  代码零硬编码绝对路径（AC-R2）。
- **工作区形态**：独立 npm 项目（package.json + node_modules），install 即得；
  基础包按 `runtime_workspace.BOOT_PEER_PACKAGES` 精确 pin 装入（0.2.1-rc.5
  + cordis 4.0.5 稳定线），npm 纪律 = 精确版本 + `--registry=registry.npmmirror.com`。
- **生命周期状态**：`<workspace>/.agentos/dsh-plugins-state.json`（enabled/
  failed/error 等，node_modules 只承载 installed 集）；管理端点与状态文件
  同源（AC-R3）。
- npm 命令执行用 `runtime_workspace.resolve_npm_binary()`（Windows 直调裸
  `npm` 名撞 WinError 2，须解析出 npm.cmd）。

### 管理页与端点（设计 §4.1 C1）

- 导航条目：`contributes.pages` 的 `dsh_plugin_manager`（workspace/tab，
  widget=webview，指向适配器自伺服 `/ext/dsh_adapter/page/manager`，与
  mode_coding 先例同构，前端零新组件）。
- 端点五件套（auth=user；设计表的 `/api/dsh/*` 逻辑路径在此落地为
  `/ext/dsh_adapter/api/dsh/plugins*`——插件端点路由前缀由内核固定）：
  `GET plugins`（实扫清单）/ `POST install`（缺省版本从索引取最新精确版本，
  禁 dist-tag；`engines.dsh` 不兼容安装前拦截；审批=human-interaction
  create_choice/wait_for_choice 复用面，卡内=翻译预览）/ `POST uninstall`
  （有依赖者拒绝）/ `POST enable|disable` / `GET index`（format 1 静态索引
  多源消费，缓存+不可达标记过期）。
- 安装走 npm `--prefix <工作区>`，包从未进工作区到装上全程先审批后落盘。

**有意跳过 DSH 侧 pre/post-execute 钩子管道**：准入由灵汐
isolation_guard/security/approval 把关，输出兜底由 tool_core 的 output_schema
校验 + spill_guard 执行（见 docs/working/dsh_hook_translation.md）。

**前端渲染**（声明翻译，非组件移植）。适配器把 DSH 工具包翻译成 `render`
声明（card 词汇表 read/terminal/search/web/diff…），由灵汐通用渲染通道
（`frontend/src/utils/renderIntent.ts` → ActivityCard 原生块）渲染。
**前端不为 DSH 写任何专属组件**——适配器递送"形态声明 + 数据"，渲染能力
由宿主统一提供，新增 DSH 卡片形态不需要改前端。

**皮肤**（主题声明 + 资产递送）。DSH 皮肤（skin-center 16 款）翻译成
`contributes.themes` 主题声明（`on_load` 自动同步进 plugin.json），
CSS / hooks.mjs / 背景资产经 `/ext/dsh_adapter/styles/**` 递送，前端皮肤
运行时按声明注入——对 DSH 零特判，加皮肤 = 放包进 `dsh_plugins/`。

### 环境要求

- Node ≥ 20 + npm（Windows 执行面经 `resolve_npm_binary()` 解析 npm.cmd）；
  首次安装自动 bootstrap 工作区基础包（精确版本 + npmmirror 镜像），也可
  手动：`npm install --prefix <工作区> @deepseek-ai/dsh-tools@0.2.1-rc.5 …`
  （全量 pin 见 `runtime_workspace.BOOT_PEER_PACKAGES`）。
- `AGENTOS_DSH_RUNTIME_ROOT` 指向 npm 运行时工作区（缺省用户空间
  `user_root()/dsh_runtime`，一般无需设置）。
- 版本升级 = 更新 pin 三写点（`translator.py` 常量 / `plugin.json`
  `contributes.dsh_adapter` / 本 README）+ 重跑桥协议测试（`pytest
  plugins/shared/dsh_adapter/tests/test_bridge_gaps.py
  test_bridge_duration_wire.py`，真机车道自动 bootstrap 工作区）。
- 历史注记：`AGENTOS_DSH_REPO_ROOT`（rc8 仓产物路径硬依赖）与
  `runtime/extra-tools/` junction 装载区已随运行时工作区机制退役；rc8 参考
  仓降级为桥协议测试夹具，不再被任何生产路径消费。钩子翻译文档
  `docs/working/dsh_hook_translation.md` 已随 2026-10-14 归档策略入包
  （`docs/工作文档归档_20261014.zip`），钩子翻译现状契约以 `translator.py`
  组 C 注释与 `docs/working/DSH生态嫁接通用机制设计_20261104.md` §4.3 为准。

## 闭环验证（output_schema 消费端）

1. LLM 调适配器工具 → tool_core 经 tool-executor → sidecar server.py 执行；
2. 返回值经 tool_core 按 `output_schema` 校验（fail-closed，违规转错误
   回传 LLM）；
3. 前端按 `render: {card: ...}` 路由到对应原生块（read 行号视图 + 窗口
   计数、search 路径平铺 + 计数等）。

## 范围外（诚实边界）

- MCP 工具：`external_mcp` 天然直连，适配器不管
- 功能型前端包（conversation/trajectory/subagent）：依赖 DSH 事件投影服务
- Cordis 运行时/slot 注册/事件投影：零引入
