# dsh_adapter — DSH 插件适配器

> task_dsh_plugin_adapter。让 DSH 的工具插件与视觉插件在灵汐稳定运行。
> **版本基准 0.2.1-rc.5**（npm next 稳定线，git tag `dsh-v0.2.1-rc.5` =
> `b830ec950f`，2026-11-04 自 0.1.0-rc.8 升级；工具契约面零漂移，见
> `docs/working/DSH适配器翻译规格_20261020.md` §2.2）。**DSH 源码零改动**
> （MIT 出处见各文件头；rc8 参考仓 `D:\reference_repos\deepseek-harness-rc8`
> 已降级为桥协议回归测试夹具——运行时不再依赖仓库产物，boot 走用户空间
> DSH profile，见下文〔运行时工作区〕节）。
> **安装管理**（2026-11-09 起）委托官方 `@deepseek-ai/dsh-plugin-manager`
> （本地目录/tarball/git/registry 原生支持，pnpm + profile 语义）。

## 结构

```
dsh_adapter/
├── plugin.json            # 2 工具契约（均带 output_schema+render）+ contributes
├── server.py              # sidecar 入口（@plugin.tool 注册面 + 皮肤递送端点）
├── translator.py          # 清单翻译器（纯函数：DSH 包 → 灵汐注册清单）
├── manager.py             # 管理服务面：审批/翻译预览/皮肤 + 官方 plugin-manager 委托
├── runtime_workspace.py   # DSH profile 工作区：脚手架/bootstrap/pnpm/状态
├── bridge.py              # Node runtime 宿主（spawn/JSON-RPC/超时/惰性boot/重启）
└── runtime/
    └── dsh-rpc-bridge.mjs # 通道 A fork：boot DSH cordis context，经 stdio 暴露工具 + manager RPC
```

## 适配面：插件带什么就适配什么

**后端工具**（通道 A）。`runtime/dsh-rpc-bridge.mjs` 从**运行时工作区**
（用户空间 DSH profile，见下节）解析 `@deepseek-ai/*` 基础包与已装 bundle，
boot 按 `bootPlan` 裁剪的最小 cordis context；另起 management context 挂
官方 plugin-manager：

```
SystemPrompt → ToolRuntime(dsh-tools) → LocalFileSystem → SubprocessLocal
→ SessionProjection → WorkingDirectory(defaultDirectory=cwd)   ← 0.2.1 链新增（tool-fs inject 依赖）
→ ToolFs(read/write/edit/…) → ToolFsSearch(glob/grep, sampleOverCapGlobResults=true)
→ profile bundle rows（官方 readProfilePatches + composeEntries 逐个 import+apply）

management context: Loader → profileContext → @deepseek-ai/dsh-plugin-manager
```

**boot 裁剪轴（AC-R4）**：`initialize {bootPlan}` 下发
`{source, fallback, chain, webServer, hooks, packages}`——`source="profile"`
时桥用官方 `readProfilePatches` + `composeEntries` 组合 bundle patch rows；
composition 失败/旧工作区回退 `fallback` 扫描。`chain:false`（空 profile/
无启用包）不 import 工具链包，initialize 返回空工具清单；webServer/钩子轴为
P1 声明位（信号面在管理端点如实报告）。

协议（newline-delimited JSON-RPC over stdio，stderr 走日志）：

| 方法 | 说明 |
|---|---|
| `initialize {cwd, bootPlan, manager?}` | boot 工具链 + management context（官方 plugin-manager）+ 返回工具契约清单 |
| `tool/call {name, args, timeoutMs}` | 直接执行（构造最小 ToolRunContext），返回灵汐信封 `{success, data, error, duration_ms}` |
| `plugin/manager/{list,inspect,install,cancel,remove,enable,disable,registries}` | 官方 plugin-manager 管理面，返回 `{ok, data, error}`（ChangeResult 原样） |
| `shutdown` | dispose 两个 context + exit 0 |

### 运行时工作区（用户空间 DSH profile，DSH 插件包=用户数据）

- **落位解析序**：显式配置 > `AGENTOS_DSH_RUNTIME_ROOT` > 用户空间默认
  （`user_root()/dsh_runtime`；dev=`<repo>/user_root/dsh_runtime/`，
  装机=`%APPDATA%\agentos\dsh_runtime\`，与内核 `core::user_space` 同源）。
  代码零硬编码绝对路径（AC-R2）。
- **工作区形态（2026-11-09 起）**：标准 DSH **profile**——
  `package.json`（`dsh.profile.bundles`）+ `cordis.yml` + `cordis.patch.yml`
  + `pnpm-workspace.yaml`（`nodeLinker: hoisted`）+ `.agentos/install-anchor/`
  （最小 installAnchor）+ `.dsh-home/`。安装/卸载/启停由官方
  `@deepseek-ai/dsh-plugin-manager` 负责（pnpm + profile 语义），不再由适配器
  手写 npm 命令。
- **bootstrap**：首次安装/管理操作自动用现有 npm 装入基础链 + 官方 manager
  栈 + `@pnpm/exe`（精确 pin，见 `runtime_workspace.PROFILE_BOOTSTRAP_PACKAGES`）；
  链包的 peer 由 npm 自动补齐（dsh-tools 等导入期硬依赖），随后跑一次
  `pnpm --dir <ws> install` 把 npm 树归一成 pnpm 布局（否则首次 `pnpm add`
  转换树时会被运行中进程持有的原生 DLL 拦住 → ERR_PNPM_EPERM）。
  `pnpm-workspace.yaml` 预置 `allowBuilds`（基础栈原生构建，npm 阶段已执行）；
  用户 bundle 自带构建脚本仍走 pnpm 待审批（`pendingBuilds` 诊断）。
  社区 bundle 的 peer（`dsh-credentials`/`dsh-commands`/`dsh-client-*` 等宿主包）
  由 pnpm `autoInstallPeers` 按声明补齐——否则装上了也会在 boot 时 import 失败。
  **直接 import 却不声明 peer** 的宿主包由装后扫描兜底：`translator.
  scan_host_imports()` 静态扫包内代码/清单，缺的按 `DSH_SOURCE_VERSION`
  精确版本经官方 `runProfilePnpm`（`plugin/host-dep/add` RPC）补装；失败
  不阻断安装，`host_deps_error` 落状态文件并在管理页显示。
  之后所有包操作走工作区内置 pnpm，不要求系统 pnpm。bootstrap 失败返回
  `node_missing/node_too_old/npm_missing/bootstrap_failed` 结构化错误。
- **宿主服务面**：最小工具链挂 SystemPrompt/ToolRuntime/FS/Subprocess/
  SessionProjection/WorkingDirectory/ToolFs，**再加 webServer 轴**
  （`@deepseek-ai/dsh-host-webserver`，有 bundle 即挂，localhost 临时端口）——
  社区 bundle 的 `inject` 常含 `webServer`（describe-image/pet），缺它插件能加载
  但工具不注册。bootstrap 另预装 `@deepseek-ai/dsh-credentials` 等宿主核心包
  （社区插件会直接 import 却不声明 peer）。
  **仍缺**：`settings`/`typert`/`sessions`+`sessionQuery`+`sessionPersistence`+
  `sessionProjectionCache`+`storageDomain`（会话存储栈）——依赖它们的插件
  （dsh-context/cost-meter 的 typert 面）只加载不激活，是后续里程碑。
- **生命周期状态**：profile 的 `dsh.profile.bundles` + node_modules 是
  installed/enabled 真值；`<workspace>/.agentos/dsh-plugins-state.json`
  只承载审批/预览/错误等 UI 元数据。官方 manager 无 HMR，变更返回
  `restart-required`；适配器随后重启桥，下次工具调用按新 profile boot。
- npm/pnpm 命令执行用 `runtime_workspace.resolve_npm_binary()` /
  `resolve_pnpm_binary()`（Windows 直调裸 `npm`/`pnpm` 名撞 WinError 2，
  须解析出 `.cmd`/`.exe`）。

### 管理页与端点（设计 §4.1 C1）

- 导航条目：`contributes.pages` 的 `dsh_plugin_manager`（workspace/tab，
  widget=webview，指向适配器自伺服 `/ext/dsh_adapter/page/manager`，与
  mode_coding 先例同构，前端零新组件）。
- 端点六件套（auth=user；设计表的 `/api/dsh/*` 逻辑路径在此落地为
  `/ext/dsh_adapter/api/dsh/plugins*`——插件端点路由前缀由内核固定）：
  `GET plugins`（官方 listBundles + 翻译预览）/ `POST install`
  （`{spec, version?}`：npm 包名 / **本地绝对路径** / `file:` / tarball /
  git 地址；官方 `inspect` 校验 + 审批卡翻译预览 + `installBundle`；
  registry 包名缺省版本从索引取最新精确版本，禁 dist-tag）/
  `POST uninstall`（官方 `removeBundle`，bundle-in-use/stop-profile 拒绝）/
  `POST enable|disable`（官方 `setBundleEnabled`）/
  `POST cancel`（官方 `cancelInstall` + 审批卡取消）/
  `GET index`（format 1 静态索引多源消费，缓存+不可达标记过期）。
- **只接受 bundle 包**：包必须声明 `dsh.bundle.patch`（官方语义）；非 bundle
  包返回 `not-bundle` 结构化错误。本地安装是官方 `inspect`/`installBundle`
  原生能力，不再是适配器特判。

**有意跳过 DSH 侧 pre/post-execute 钩子管道**：准入由灵汐
isolation_guard/security/approval 把关，输出兜底由 tool_core 的 output_schema
校验 + spill_guard 执行（见 docs/working/dsh_hook_translation.md）。

**前端渲染**（声明翻译，非组件移植）。适配器把 DSH 工具包翻译成 `render`
声明（card 词汇表 read/terminal/search/web/diff…），由灵汐通用渲染通道
（`frontend/src/utils/renderIntent.ts` → ActivityCard 原生块）渲染。
`on_load` 还会把已装 DSH 包的 toolview 键聚合进 `contributes.renderers`
（`{tool, card}`，白名单与前端 `DSH_RENDER_CARDS` 同源），前端 dshAdapter
服务据此注册 render 意图兜底——DSH 工具装上即可走灵汐原生卡片。
**前端不为 DSH 写任何专属组件**——适配器递送"形态声明 + 数据"，渲染能力
由宿主统一提供，新增 DSH 卡片形态不需要改前端。挂载位/渲染位全表见
`docs/working/DSH_挂载与渲染位对照表.md`。

**皮肤**（主题声明 + 资产递送）。DSH 皮肤（skin-center 16 款）翻译成
`contributes.themes` 主题声明（`on_load` 自动同步进 plugin.json），
CSS / hooks.mjs / 背景资产经 `/ext/dsh_adapter/styles/**` 递送，前端皮肤
运行时按声明注入——对 DSH 零特判，加皮肤 = 放包进 `dsh_plugins/`。

### 环境要求

- **Node ≥ 22** + npm（官方 plugin-manager 的 execa 10 要求；Windows 执行面经
  `resolve_npm_binary()` 解析 npm.cmd）。首次安装自动 bootstrap 工作区
  （基础链 + 官方 manager 栈 + `@pnpm/exe`，精确版本 + npmmirror 镜像）；
  之后包操作走工作区内置 pnpm，**不要求系统 pnpm**。
- `AGENTOS_DSH_RUNTIME_ROOT` 指向 DSH profile 工作区（缺省用户空间
  `user_root()/dsh_runtime`，一般无需设置）。
- 版本升级 = 更新 pin 四写点（`translator.py` 常量 / `runtime_workspace.py`
  `MANAGER_PACKAGES` / `plugin.json` `contributes.dsh_adapter` / 本 README）
  + 重跑桥协议测试（`pytest plugins/shared/dsh_adapter/tests/test_bridge_gaps.py
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
