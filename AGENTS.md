# 灵汐 AgentOS — 项目记忆（供 AI 协作者）

> 本文件是给在本仓库工作的 AI 代理（DSH 等）的项目上下文。读完它再动手；
> 与本文件冲突的旧文档（README 等 0.1 口径描述）以本文件 + `docs/decisions/` 为准。

## 项目是什么

**可进化的智能体操作系统**：把 LLM、工具、记忆、任务、配置组织成可观测、可干预、
可回滚的管道（Pipeline）。当前主线为 **0.2 架构**：Rust 微内核 + Python 插件 +
React 前端。

## 仓库布局（0.2 现实）

| 路径 | 内容 |
|---|---|
| `kernel/` | Rust 微内核（crates：api/config/core/db-admin/engine/hooks/http/invoker/mcp/plugin-loader/session/tenant/user-admin；`agentos-kernel` 是主进程） |
| `plugins/` | Python 插件（`shared/system/` 系统插件、`shared/tools/` 工具插件、`shared/pipeline/` 管道插件） |
| `frontend/src/` | React 19 + Vite + Zustand + Antd（RJSF v6 表单引擎） |
| `config/` | 运行配置（agents/tools/plugins/pipelines/isolation/storage.yaml 等） |
| `docs/` | 文档；`docs/decisions/` = ADR 决策记录；`docs/working/` = 工作文档与研究报告 |
| `tests/` | Python 测试（pytest）；`kernel/crates/*/tests/` Rust 测试 |
| `scripts/` | 运维/清理脚本 |
| `data/`、`logs/`、`reports/` | 运行时产物 |

存储：SQLite（默认项目根 `agentos_kernel.db`），driver 化切换见
`config/storage.yaml`（`AGENTOS_STORAGE_DRIVER`/`AGENTOS_DB_PATH` 环境变量可覆盖）。

## ⚠️ 工作区铁律（最重要）

**本仓库工作区会被周期性还原到 git HEAD——未提交的改动会被抹掉。**
任何修改（含中间产物、测试数据）完成后**立即 commit**，不要留到"最后一起提交"。
commit 前的调查/验证工作尽量用未跟踪文件（`??` 状态）或文档目录进行。

## 架构要点（2026-08 现状）

- **插件即声明**：插件 = `plugin.json` 清单 + Python 实现。`capabilities.tools` =
  LLM 工具**声明即注册**（无需类型豁免）；`capabilities.services` = 内部服务方法元数据。
  工具声明要带 `output_schema` + `render`（工具契约，tool_core 校验 fail-closed，
  前端按 render 意图路由）。插件放 `plugins/shared/{system,tools,pipeline}/<name>/`。
- **内核只落库**：评估闸门判定已出内核——插件裁决（`evaluation/`），内核只记录结果。
  任务完成必须过评估（无证据 → `pending_evaluation`）。agent 配置也出内核
  （`context_build` 自持加载）。
- **任务默认隔离执行**：默认工作空间 `workspace/{task_id}` + isolated。
- **工具面过滤**：LLM 可见工具由 `config/agents/main/agentos.yaml`（及
  `executor/general_agent.yaml`）的 `tool_ids` 白名单控制，新工具记得加入。
- **权限模式**：5 种权限模式 + 参数级危险判定，纯插件前端（http_endpoints + form compact +
  human-interaction 确认）。会话隔离由 isolation_guard 容器落地。
- **插件热发现**：新建/修改插件后内核热加载 manifest（5-8s），但 `enabled_plugin_ids`
  是启动期快照——新插件需 reenable 重注册；前端 schema 需刷新页面才更新。
- **多循环体/执行上下文**：`execution_context` 贯穿任务链（agent_id 全链传导）。

## 约定

- **ADR 制度**：任何非平凡决策必须写 `docs/decisions/YYYY-MM-DD-<slug>.md`
  （背景/决策/Alternatives Considered/影响/归档五段），**强制记录被否方案**。
- **契约冻结**：0.2 定型后接口契约尽量冻结（能不动就不动）；动契约需 ADR + 兼容机制。
- **被否方案索引**：`ROADMAP.md` 含 6 大类被否方案索引（90+ 项），动手前先查。
- **决策记录**：DSH 借鉴 → 本仓 `docs/dsh_decision_records.md`；插件生态评估 →
  `docs/plugin-protocol.md` 等。
- **门禁**：机械门禁优先（测试/格式/覆盖率）；改造后必须本地补跑全部可验证车道，
  并如实区分"门禁绿"与"测试绿"。
- **触碰即清（治理债随模块清，2026-08-19 用户要求）**：改动触碰某模块
  （plugins/shared / kernel/crates / frontend/src）时，同刀清该模块对应的治理债：
  Python 模块 mypy 错误不增、清了就下调 `.github/mypy-baseline.txt`（现值 0，
  只减不增）；修好基线内既有红测试就收紧 `.github/pytest-failure-baseline.txt`
  （plugins-coverage 19 / plugins-heavy 0）；新代码带测试（覆盖率棘轮门禁
  兜底，2026-08-20 ADR：整体覆盖率基线只升不降且**略高于实测留压力**——
  Python 64.0/Rust 86.0/前端待校准，改动行覆盖率 100%（diff coverage，
  `[skip-diff-cov]` 逃生口），检查器 `scripts/check_*_coverage_baseline.py` +
  `scripts/check_diff_coverage.py`）。
  基线文件改动一律走 commit 留归因。细则与三问清单：
  docs/working/长期治理债清理方案_20260819.md。

## 测试

- Python：`pytest`（部分车道需环境变量，如 DSH e2e = `AGENTOS_DSH_E2E=1`）。
- Rust：`cargo test`（kernel/ 下）。
- 前端：vitest。
- 既有测试在 0.2 迁移后部分断裂属已知，修功能前先确认目标车道基线。

## DSH 适配器（本仓与 DeepSeek Harness 的桥）

- `plugins/shared/system/dsh_adapter/`：Node runtime 桥（dsh_read/dsh_glob 等工具）、
  DSH 插件包装载（`dsh_plugins/` 子目录，npm 解压物原样放）、清单翻译。
- **DSH 源码零改动**（只读参考 `D:\reference_repos\deepseek-harness-rc8`，commit 141eb6fe 锁定；旧 rc.5 git 检出保留在 `D:\reference_repos\deepseek-harness`）。
- 加装 DSH 插件 = 放 `dsh_plugins/` 子目录；逐包启停 = `config/dsh_adapter.yaml`。
- 升级 DSH = 重跑适配器 e2e + 更新 plugin.json 锁定契约。
- 当前分支：`dev/0.2`（PR 目标 `main`）。

## 常用操作速查

- 加工具插件：`plugins/shared/tools/<name>/`（plugin.json 声明 + 实现 + 测试），
  加入 agentos.yaml 的 tool_ids，补 output_schema/render。
- 加系统插件：`plugins/shared/system/<name>/`。
- 改 agent 配置：`config/agents/main/`（agentos.yaml + persona/ + 提示词骨架）。
- 查架构决策：`docs/decisions/` 按日期排序；被否方案先查 ROADMAP.md。
- 本项目文档入口：`docs/` 下 ARCHITECTURE.md / vision.md / plugin-protocol.md 等。
