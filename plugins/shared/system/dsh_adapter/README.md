# dsh_adapter — DSH 插件适配器

> task_dsh_plugin_adapter。让 DSH 的工具插件与视觉插件在灵汐稳定运行。
> **DSH 源码零改动**（只读参考 `D:\reference_repos\deepseek-harness-rc8`，
> commit `141eb6fe` / 0.1.0-rc.8 锁定，MIT 出处见各文件头；2026-08-21 自
> rc.5 升级，gh api tarball 路线——git 直连 github.com 不通时用
> `gh api repos/deepseek-ai/deepseek-harness/tarball/<sha>` 拉源码快照，
> `pnpm install --frozen-lockfile --registry=npmmirror` + `pnpm build:lib:host`）。

## 结构

```
dsh_adapter/
├── plugin.json            # 4 工具契约（dsh_read/dsh_glob 带 output_schema+render）+ contributes
├── server.py              # sidecar 入口（@plugin.tool 注册面 + 皮肤递送端点）
├── translator.py          # 清单翻译器（纯函数：DSH 包 → 灵汐注册清单）
├── bridge.py              # Node runtime 宿主（spawn/JSON-RPC/超时/惰性boot/重启）
└── runtime/
    └── dsh-rpc-bridge.mjs # 通道 A fork：boot DSH cordis context，经 stdio 暴露工具
```

## 适配面：插件带什么就适配什么

**后端工具**（通道 A）。`runtime/dsh-rpc-bridge.mjs` 以**绝对路径导入** DSH
仓库已构建产物（`apps/cli/node_modules/@deepseek-ai/*`），boot 最小 cordis
context：

```
SystemPrompt → ToolRuntime(dsh-tools) → LocalFileSystem → SubprocessLocal
→ ToolFs(read/write/edit/…) → ToolFsSearch(glob/grep, sampleOverCapGlobResults=true)
```

协议（newline-delimited JSON-RPC over stdio，stderr 走日志）：

| 方法 | 说明 |
|---|---|
| `initialize {cwd}` | boot context + 返回工具契约清单（name/description/input/output schema） |
| `tool/call {name, args, timeoutMs}` | 直接执行（构造最小 ToolRunContext），返回灵汐信封 `{success, data, error, duration_ms}` |
| `shutdown` | dispose + exit 0 |

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

- Node ≥ 20；`AGENTOS_DSH_REPO_ROOT` 指向已构建的 deepseek-harness 仓库
  （默认 `D:\reference_repos\deepseek-harness-rc8`，需含 `apps/cli/node_modules`）
- DSH 仓库升级 = 重跑 e2e（`AGENTOS_DSH_E2E=1 pytest
  plugins/shared/system/dsh_adapter/tests/test_dsh_adapter.py`）+ 更新 plugin.json 锁定契约

## 闭环验证（output_schema 消费端）

1. LLM 调 `dsh_read` → tool_core 经 tool-executor → sidecar server.py →
   bridge → Node DSH `read` 真实执行；
2. 返回值经 tool_core 按 `output_schema` 校验（fail-closed，违规转错误
   回传 LLM）；
3. 前端按 `render: {card: "read"}` 路由到原生行号视图（行号 gutter +
   窗口计数），`dsh_glob` 路由到原生搜索结果块（路径平铺 + 计数）。

## 范围外（诚实边界）

- MCP 工具：`external_mcp` 天然直连，适配器不管
- 功能型前端包（conversation/trajectory/subagent）：依赖 DSH 事件投影服务
- Cordis 运行时/slot 注册/事件投影：零引入
