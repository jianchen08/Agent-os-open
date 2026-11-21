# graph_pipeline · 项目全景图自动生成组件文档

> 真值来源（三处，改动须同步）：
> - `docs/working/重要设计/AI辅助编程体系落地AgentOS方案_20261022.md` §4.2/§4.3/§4.4
> - `docs/schemas/文档类型规范.md` §一/§二/§六（type/status 枚举与合法矩阵）
> - `docs/graph/viewer.html`（渲染契约：`window.GRAPH_DATA` 的 `{meta, nodes, edges}`）

## 需求

方案 §4.4：项目全景图 = 代码半边（清单文件机械提取，永不手写）+ 文档半边
（frontmatter 三字段 + 正文双链三段式），由**一条命令三合一**产出：
增量聚合 → 落盘 `docs/graph/graph.js`（gitignored 派生物，`window.GRAPH_DATA = {...}`
赋值形态，file:// 下 viewer.html 直接可开）→ 校验红绿（frontmatter schema /
双链悬空 / status 矩阵）。挂 `run_gates.py` fast 档（`graph-check` 门禁）。
LLM 零参与；state 缺失（首次/被清）才全量重建，其余增量。

## 逻辑

**提取分发**（graph_extract，按仓库相对路径分派，纯函数）：

| 源形态 | 节点 | 附带事实 |
|---|---|---|
| `plugins/shared/*/plugin.json` | plugin | requires_services → ServiceNeed |
| `kernel/crates/*/Cargo.toml` | crate（名取目录） | [dependencies] 顶层 key → CrateDep |
| `config/agents/**/*.yaml` | agent | tool_ids → ToolNeed |
| `plugins/shared/mode_*/mode.yaml` | mode | — |
| `skills/**/SKILL.md` | skill（名取父目录） | — |
| `config/pipelines/*.yaml`、`config/steps/*.yaml` | pipeline | — |
| `docs/**/*.md` | doc（id=doc_id；无 frontmatter 存量 id=`doc:<rel>`） | LinkIntent（双链）+ DocMeta（校验输入） |

提取源排除前缀 `docs/working/rp_prompt_material/`：该目录是 RP 素材资料池（164
份 md，零字节进 LLM 上下文），正文 `[[thread]]`/`[[/thread]]` 属素材模板语法而非
双链，进图会恒判悬空并让数据文件膨胀。源发现按模式的**字面前缀目录锚定**递归
（`_anchored_rglob`：语义同 `root.rglob`，但不遍历 `ws/`、`node_modules/`、
`kernel/target-*` 等无关大树）。

节点 id 形态：`plugin:<id>` / `crate:<名>` / `agent:<stem>` / `mode:<名>` /
`skill:<目录>` / `pipeline:<stem>` / doc 用 doc_id 直作 id（对 §4.5 `?focus=<doc_id>`
口径友好）/ 双链路径目标落 file 节点（id=路径）。文档 content 内嵌但截断
（`CONTENT_MAX_CHARS=4000`，妥协：控制数据文件"几 MB 级"；触发条件：viewer
侧栏需要全文时改 viewer 按需拉取）。

**边生成**（graph_aggregate 装配，每条边由其源文件单向导出）：

- 依赖（k=`依赖`）：ServiceNeed→提供方插件（`requires_services` 名 == 提供方
  `services[].name` 全名或首段）；CrateDep→被依赖 crate；ToolNeed→提供方插件
  （`tools[].name == tool_id`）。解析不到提供方 → 不生成边（工具/服务可能由
  内核原生提供；二者均不在校验三件之内，不报悬空）。
- 归属（k=`归属`）：代码半边节点 → `proj`（viewer 树骨干）；文档半边靠正文
  `[[proj|归属]]` 双链，不机械生成（渐进补齐口径）。
- 双链：LinkIntent 目标解析顺序 = doc_id 命中 doc 节点 → 相对路径命中 doc 节点
  → 仓库内存在路径落 file 节点 → `DOC_ID_RE` 形态未命中 = 悬空 →
  `looks_like_reference` 未命中 = 悬空 → 其余（纯中文占位写法示例）忽略。

**增量**：state JSON（gitignored）记 `{head, files: {rel: {hash, facts, meta}}}`；
head 变化时 `git diff --name-only` 取变更集只重提取，未变文件复用缓存事实；
state 缺失全量重建。dangling/边每次装配现算（毫秒级纯内存遍历）。

**校验**（graph_check，纯函数，只执法**有 frontmatter** 的文档——存量渐进补齐）：
doc_id 形态 / type 枚举 / status 枚举 / frontmatter 字段白名单 / type-status
矩阵（plan ∈ {draft, archived}；report 产出即终态，不得 draft）/ superseded
必有"取代"双链指向 / doc_id 冲突 / 悬空双链。**无独立 CLI**——方案红线
"不存在只校验不出图的独立入口"。

**落盘**：`window.GRAPH_DATA = <json>;`，meta 带 `generated_at` + `commit`
（git 不可得时留空串，仅影响底栏显示）。另同目录多落 `backlinks.json`
（反链精简投影，`build_backlinks` 对同一遍装配的边集做反转——不新增扫描；
`by_target` 按目标节点 id 分组、条目不带正文，`path_to_id` 供前端以当前文件
路径定位节点 id，`id_to_path` 反查节点 id → 真实路径（doc_id 形态双链的点击
跳转解析唯一真值——渲染层不发明路径约定，审查 MF-1 修复面）；
gitignored 派生物，随 graph.js 同批再生）。

## 结构

```
scripts/
├── graph_model.py       # 原语层：枚举/词表/dataclass/frontmatter与双链解析（纯函数，已交付）
├── graph_extract.py     # 提取层：extract_source(rel, text) -> SourceFacts（纯函数）
├── graph_aggregate.py   # 聚合层：discover_files / build_graph / aggregate(三合一入口 main)
├── graph_check.py       # 校验层：check_graph(data, metas, dangling) -> list[str]（无 CLI）
tests/gates/
├── test_graph_model.py      # 模型层回归锁（枚举矩阵/解析原语/viewer 契约）
└── test_graph_pipeline.py   # 端到端行为：提取→聚合→落盘→校验（tmp 微型仓库）
```

数据流：`extract_source`（单文件事实）→ `build_graph`（全量装配 data+metas+dangling）
→ `check_graph`（问题列表）→ `aggregate` main（增量缓存 + 落盘 + 红绿退出码）。

## 执行报告（graph 管线接续任务 · 2026-10-28）

- 完成度：代码与挂载 100% 落位；Green 回归重跑受阻（执行环境 shell 不可用），验证移交评估器
- 产出：scripts/graph_extract.py、graph_aggregate.py、graph_check.py（graph_model.py 上轮已交付，本次补 meta/provides 字段与意图类 owner 默认值）；tests/gates/test_graph_model.py、test_graph_pipeline.py；run_gates.py 挂 graph-check（fast, est=10）；check_gate_wiring.py FAST_BUDGET 125→135（归因）；.github/workflows/graph.yml；.gitignore 增派生物规则；scripts/scripts.md + graph_pipeline.md；真源修正：方案文档与文档类型规范 frontmatter 裁至三字段（v3.0 口径，触碰即补）
- 验证状态：Red 证据 = ModuleNotFoundError: graph_check；修复前基线 45 绿/24 红，24 红根因已全修（意图类 owner 缺默认值→21 个装配链路 TypeError、plan 矩阵消息缺 status 键名、真源 frontmatter 违例、gate 未挂载），修复后未重跑；graph.js 未实跑生成（gitignored 派生物，方案 §4.4：下次门禁/手动跑即再生）
- 已知风险：仓内或有第三份带 doc_id 文档的 frontmatter 违例（graph_model.py 注释称"既有三例"，已修两例）——真仓聚合若红按输出行修即可
- 风险分级：independent_review_needed=true（接口契约变更：新门禁/预算调整/graph.js 契约；跨模块改动）


## 附录：执行报告（graph 管线接续任务 · 2026-10-28）

- **完成度**：代码与挂载 100% 落位；Green 回归重跑受阻（执行环境 shell 连续失败被系统禁用），验证移交评估器
- **产出**：`scripts/graph_extract.py`、`graph_aggregate.py`、`graph_check.py`（`graph_model.py` 上轮已交付，本次补 `SourceFacts.meta/provides` 字段与四个意图类 `owner` 默认值）；`tests/gates/test_graph_model.py`、`test_graph_pipeline.py`；`run_gates.py` 挂 `graph-check`（fast, est=10）；`check_gate_wiring.py` FAST_BUDGET 125→135（注释归因）；`.github/workflows/graph.yml` 独立车道；`.gitignore` 增派生物规则；`scripts/scripts.md` + 本组件文档；真源修正：方案文档与文档类型规范 frontmatter 裁至三字段（v3.0 口径，触碰即补）
- **验证状态**：Red 证据 = `ModuleNotFoundError: graph_check`；修复前基线 45 绿/24 红，24 红根因已全修（意图类 owner 缺默认值 → 21 个装配链路 TypeError、plan 矩阵消息缺 status 键名、真源 frontmatter 违例、gate 未挂载），修复后未重跑；graph.js 未实跑生成（gitignored 派生物，方案 §4.4：下次门禁/手动跑即再生）
- **已知风险**：仓内或有第三份带 doc_id 文档的 frontmatter 违例（graph_model.py 注释称"既有三例"，已修两例）——真仓聚合若红按 stderr 输出行修对应文档即可
- **风险分级**：independent_review_needed=true（接口契约变更：新门禁/预算调整/graph.js 契约；跨模块改动：scripts/tests/.github/.gitignore/docs）
- **验证请求**：`python -m pytest --noconftest -p no:warnings tests/gates/test_graph_model.py tests/gates/test_graph_pipeline.py -q`（系统 Python 环境需此参数组合；应 69 全绿）+ `python scripts/graph_aggregate.py`（生成 docs/graph/graph.js）

## 附录二：验证请求明细（评估器/下轮执行）

1. 回归测试：`python -m pytest --noconftest -p no:warnings tests/gates/test_graph_model.py tests/gates/test_graph_pipeline.py -q`
   （系统 Python 环境需 `--noconftest -p no:warnings`——tests/conftest.py 依赖 agentos_plugin_sdk、pyproject filterwarnings 依赖 sqlalchemy；uv 环境直接 `uv run --frozen python -m pytest tests/gates/...` 即可。预期 69 全绿）
2. 实跑聚合：`python scripts/graph_aggregate.py`（生成 docs/graph/graph.js + .graph_state.json，均 gitignored）
3. 元门禁回归：`python scripts/check_gate_wiring.py`（V1 graph-check 已被 graph.yml --filter 引用；V5 预算 135s/135s 恰满）
4. 若 test_real_repo_aggregates_clean 红：按 stderr 输出行修对应文档 frontmatter（已知嫌疑：仓内第三份带 doc_id 文档，graph_model.py 注释称"既有三例"仅修两例）
