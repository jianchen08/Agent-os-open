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
- **评估闸门：插件判定，内核只落库**：判定逻辑已从内核**移出**归插件——评估裁决
  在 `plugins/shared/system/evaluation/`（task_evaluate 工具），放行检测在管道
  output 步骤 task_reminder（提醒耗尽仍无评估证据 → `task.status =
  pending_evaluation`，不落 completed；有证据内核才补落默认 completed）；内核不
  做判定，只经 pipeline-state 写面记录结果。agent 配置加载同样已移出内核，由
  管道输入插件 `context_build` 自持（`plugins/shared/pipeline/input/context_build/`）。
- **任务默认隔离执行**：默认工作空间 `workspace/{task_id}` + isolated。
- **工具面过滤**：LLM 可见工具由 `config/agents/main/agentos.yaml`（及
  `executor/general_agent.yaml`）的 `tool_ids` 白名单控制，新工具记得加入。
- **权限模式**：5 种权限模式 + 参数级危险判定，纯插件前端（http_endpoints + form compact +
  human-interaction 确认）。会话隔离由 isolation_guard 容器落地。
- **插件热发现/热重载全链路**：新建插件目录、修改 plugin.json、改插件 Python 代码
  均由 watcher 自动处理（发现→G2 校验→注册/重注册/respawn），无需 re-enable 或重启；
  cdylib 集合变更走 G8 自动重启（同 id 换产物保守重启）；已知插件面取自共享
  manifests 活集合（新插件热注册后管道引用即可编译）；前端 schema 需刷新页面才更新。
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

## 编码纪律（编码任务常驻）

- **需求第一，一字不增**：只实现需求明确描述的功能；区分"不言自明的系统需求"
  （操作连贯性/状态反馈/安全底线）与"有争议的体验假设"（用户没说的一律不做），
  禁止"用户可能想…"式假设。
- **最小输出与优先修改**：满足需求前提下优先减少/修改现有代码而非新增；禁止冗余
  （多余 try-catch、空 else、假 fallback、TODO）；写新代码前先查现有接口优先复用；
  绝不访问模块私有成员；遇阻碍先沟通，禁止静默绕过或重写已有 API；两处只有小段
  不同时提取公共部分参数化差异点，不整段复制（复制必漂移）。
- **错误处理铁律**：要么处理要么传播，禁止吞异常、空 catch 或仅日志记录；错误是值、
  应可恢复；边界处翻译错误，日志与用户提示分离。
- **Bug 修复安全（止血不截肢）**：修复必须定位并修改精确错误，严禁退回旧方案或
  绕开；重构须确保外部行为不变。
- **注释只写现状契约**：功能契约/不变量/"为什么这样写才对"保留；翻译式注释（仅
  翻译代码动作）删；历史叙事/bug 故事（"曾/之前/修复了"）改写为现状——那是
  git blame 与 commit message 的职责；`[来源:]`/`[未验证]` 标记保留。
- **测试断行为不断实现**：断言可观察行为（输入→输出/副作用），不断言内部细节
  （`mock.call_count`/私有方法）；mock 仅用于外部依赖（第三方 API/数据库/网络/
  时钟/随机数/文件系统），过度 mock 内部依赖视为违规；时序测试禁止零延迟 mock，
  须 fake clock 注入可调延迟断言时序不变量。
- **测试防拟合**：同一行为 ≥2 组有区分度输入（正常/边界/符号量级相反）；字面值
  断言须配 ≥1 条性质断言（范围/单调/幂等）；可枚举输入用 parametrize 展开；关键
  路径至少一条走真实依赖，禁止全 mock；实现禁止硬编码期望值刷绿——泛化性自检
  （测试集外同契约输入仍正确）+ 删值实验（改疑似硬编码常量，测试必须变红）。
- **兼容性一刀切**：内部代码（消费方可全量感知，本仓均属此类）直接改、不留兼容层；
  仅外部依赖（第三方 API/已发布接口）才兼容降级——字段只增不删、Deprecate→Warn→
  Remove 渐进、禁止静默降级（核心流程阻塞报错，非核心给降级提示）。
- **量化阈值**：嵌套 ≤4 层、函数圈复杂度/认知复杂度 >25、函数体 >200 行、新增
  Bug/漏洞 >0 均 Must Fix；覆盖率分级 P0 核心逻辑 100% 分支 / P1 公共服务工具
  90% / P2 一般代码 80%。
- **反模式红线**：硬编码密钥/URL；无注解 any / `@ts-ignore`（确实无法标注须
  `// HACK: <原因>`）；隐性技术债务（简化实现必须写明妥协内容+升级触发条件+时间
  上限）；调试日志残留（任务结束前清理）；无测试提交；方案倒退（用旧方案须说明
  为何该场景下正确）。

## 测试

- Python：`pytest`（部分车道需环境变量，如 DSH e2e = `AGENTOS_DSH_E2E=1`）。
- Rust：`cargo test`（kernel/ 下）。
- 前端：vitest。
- 既有测试在 0.2 迁移后部分断裂属已知，修功能前先确认目标车道基线。

## 常用操作速查

- 加工具插件：`plugins/shared/tools/<name>/`（plugin.json 声明 + 实现 + 测试），
  加入 agentos.yaml 的 tool_ids，补 output_schema/render。
- 加系统插件：`plugins/shared/system/<name>/`。
- 改 agent 配置：`config/agents/main/`（agentos.yaml + persona/ + 提示词骨架）。
- 查架构决策：`docs/decisions/` 按日期排序；被否方案先查 ROADMAP.md。
- DSH 适配器（源码零改动、插件装载、升级）：`plugins/shared/system/dsh_adapter/`，
  操作路径与决策见 `docs/dsh_decision_records.md`。
- 本项目文档入口：`docs/` 下 ARCHITECTURE.md / vision.md / plugin-protocol.md 等。
