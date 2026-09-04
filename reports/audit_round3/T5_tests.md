# T5 全项目测试体系审查（Round 3 全新独立审查）

> 审查基准：`config/rules/testing_rules.md`、`config/rules/per_domain/coding_domain_rules.md` §6/§9、`config/rules/per_agent/code_reviewer_rules.md`。
> 独立性声明：本报告从零开始，**仅依据代码现状与规则**，未读 `reports/` 下任何既有文件。
> 审查时间：2026-08-12。仓库：`D:\myproject\container_e17cc5927dfd`（Windows / Git Bash）。

---

## ① 范围与方法

**范围**：
- Python：`tests/`（顶层 + 子目录）、`plugins/`（含 `plugins/sdk/`、`plugins/shared/**`、`plugins/test_system_plugins.py`）
- Rust：`kernel/crates/*/tests/` + 各 crate `src/*.rs` 内嵌 `#[cfg(test)]`
- 前端：`frontend/src/**/*.test.{ts,tsx}` + `frontend/e2e/specs/*.spec.ts`
- CI 真实覆盖：`.github/workflows/ci.yml`（仓库内唯一 workflow）

**方法**（所有命令加 timeout 规避网络中断）：
1. 文件级清点：`find tests -name test_*.py | wc -l`（157）、`find kernel/crates -path "*/tests/*.rs"`（43）、`find frontend -name "*.test.ts*" -not -path "*/node_modules/*"`（151，其中 e2e 14）。
2. pytest 收集：`python -m pytest tests/ --collect-only -q`（实测 2488 用例 / 1 个 collection error）。
3. 标记分布：`grep -rh "pytest.mark.\w+" tests/ plugins/` 按类型计数。
4. 禁止行为扫描：`waitForTimeout` / `time.sleep` / `call_count ==` / `assert_called` / `querySelector` / 无断言函数体扫描。
5. **CI 实测**：逐 job 读 `.github/workflows/ci.yml`，对照每个 job 的 `working-directory` 与 `run` 命令，逐一核对路径真实存在与否（`for p in ...; do [ -e "$p" ] ...`）。
6. 抽样精读：`tests/channels/test_channel_gateway.py`、`tests/test_track_cost_update_event.py`、`kernel/crates/api/tests/actions_execute_test.rs`、`kernel/crates/integration-tests/tests/bench_baseline.rs`、`frontend/src/stores/__tests__/appendMessagesClientIdDedup.test.ts`、`frontend/e2e/specs/ac_validation.spec.ts`。

---

## ② 规模与金字塔

### 2.1 规模（实测）

| 维度 | 文件数 | 备注 |
|------|--------|------|
| Python `tests/` | 157 | 含顶层 64 + 子目录 93；pytest 实收 2488 用例 |
| Rust `kernel/crates/*/tests/` | 43 | 9 个 crate 有 tests/；4 个 crate（invoker/tenant/native-sdk/hooks）仅靠 src 内嵌 cfg(test) |
| 前端 vitest（src） | 137 | `frontend/src/**/*.test.{ts,tsx}` |
| 前端 Playwright（e2e） | 14 | `frontend/e2e/specs/*.spec.ts` |
| **合计** | **351** | — |

### 2.2 金字塔占比（按文件数结构估算）

规则要求 **单元 70% / 集成 20% / E2E 10%**。实测结构估算：

| 层 | 估算文件 | 占比 | 目标 | 偏差 |
|----|----------|------|------|------|
| 单元 | ~175（FE src 137 + py unit/channels/tools/sdk ~28 + rust 内嵌 ~10） | ~50% | 70% | **偏低** |
| 集成 | ~150（py 顶层 64 + suites/channels/connectors ~70 + rust tests/ 43 中大多数 + FE 部分） | ~43% | 20% | **明显偏高** |
| E2E | ~26（py e2e/e2e_02/manual ~19 + FE e2e 14） | ~7% | 10% | 略低 |

**结论**：金字塔呈"中部臃肿"——集成层（含大量 Rust 端到端 router 测试 + Python 顶层回归测试）占比远超 20% 目标；单元层因前端组件测试归类与 Python 单元稀少（`tests/unit/` 仅 5 文件）而偏低。但**真正的系统性问题不在比例，而在 CI 覆盖**（见 ⑧），未跑的测试再多也不构成门禁。

### 2.3 `tests/integration/` 名实不符

`tests/integration/` 仅含 `conftest.py` + `__init__.py`，**0 个 test 文件**（`__pycache__` 残留显示曾有 `test_t1_t4_integration.py` / `test_t5_comprehensive.py` 已被删除）。金字塔中层在该目录下视觉上存在、实质上空缺，属误导。

---

## ③ 命名

**总体良好**。Python 文件统一 `test_{模块名}.py`；测试类 `Test{被测类名}`；测试函数多采用 `test_{方法}_{场景}_{预期}`（如 `test_actions_execute_unknown_command_returns_404`、`test_register_duplicate_adapter_raises`）。

抽样的"意图式命名"普遍到位：`test_场景1: 乐观 user + appendMessages 推回 API user → user 仅一条`（frontend）、`test_handle_message_normalizes_and_routes`（python）。

**少量瑕疵**：
- 全仓 `def test_[0-9_]` / `def test_test_` / `def test_foo` 等劣质命名扫描结果 0 处（良好）。
- Rust 测试函数命名普遍完整表达"前提→预期"（如 `test_actions_execute_unknown_command_returns_404`）。

---

## ④ Mock 策略

### 4.1 Python（unittest.mock / pytest-mock）

- **使用面**：`assert_called*` 在 `tests/channels/` 集中（feishu/dingtalk/gateway 共 ~25 处），mock 对象为外部适配器（飞书/钉钉 HTTP 客户端、stream_client）—— **属"外部依赖"边界，mock 合理**。
- **越界样本**：`tests/channels/test_channel_gateway.py:54-66` 用 `mock_adapter1.start.assert_called_once()` 断言内部协作者（ChannelAdapter）被调几次。Adapter 虽是"外部渠道"的抽象，但此处被测对象是 ChannelGateway 自身、Adapter 是其内部协作者——按 §9.5 这种"内部调用次数"断言属噪声，重构即脆裂（详见问题清单 #10）。
- **call_count == N 硬断言**：扫描 `call_count ==` 在 tests/ 与 plugins/ 下 0 处直击（注：`assert_called` 变体存在 25+ 处，但都是 `assert_called_once`，未发现 `== 3` 这类次数硬钉死）。整体克制。

### 4.2 Rust

- **不使用 mockall/mock! 框架**（全 `kernel/crates/*/tests/` 0 处引用）。Rust 测试一律用真实组件 + `tower::ServiceExt::oneshot` 真发 HTTP + `NoopInvoker`/最小 `PluginManifest` 字面量等手写 fake。**符合 §6.3"行为测试原则"**——这种风格天然避开"mock 内部模块"陷阱。
- 唯一风险：`bench_baseline.rs` 使用真实 `std::time::Instant`（见 ⑦ §9.4）。

### 4.3 前端

- 77 个 src 测试文件使用 `vi.mock`（多为 mock `@/services/api`、`next/navigation`、`@/stores/...`）。抽样的 `appendMessagesClientIdDedup.test.ts` 不 mock 被测 store 本身、只初始化真实 state 后断言可观察 messages 数组——**行为测试范式正确**。
- 少量 `.mock.calls[0][1]` 直接读 mock 调用参数（`ConversationNavigateTab.test.tsx:97-114`、`FormWidget.test.tsx:250-318`、`api_alignment.test.ts:279/304`）。`api_alignment` 断言传给 axios 的 URL 属契约（可接受）；`ConversationNavigateTab` 断言 `navigateToTab(tabId)` 的入参，在已有可观察 DOM/URL 的情况下属实现细节（详见问题清单 #13）。
- 65 个 src 文件使用/测试 `data-testid`——选择器风格总体健康。

---

## ⑤ 禁止行为（rules §7）

| 禁止项 | 实测 | 评级 |
|--------|------|------|
| **测试无断言** | 全仓 `test_*.py` 函数体扫描，仅 1 处疑似无断言（`tests/tools/builtin/bash/test_cleanup_on_complete.py:86 test_on_output_task_done_unknown_pid_no_error`，2 行体）。前端无纯 smoke 命中。 | 良好 |
| **固定 sleep（Python）** | `time.sleep` / `asyncio.sleep` 共 **107 处**。多数集中在 `tests/e2e/`、`tests/e2e_02/`、`tests/manual/`（手动 e2e，可放宽）；但 `tests/suites/core/test_pipeline_stability.py:77`、`tests/suites/llm/test_keypool_selection.py:68/114`（`asyncio.sleep(0.05)` 用于并发时序）属时序测试用固定延迟，违反 §6.4。 | Should Fix |
| **固定 sleep（前端 e2e）** | `waitForTimeout` 共 13 文件命中、50+ 处，含 `ac_validation.spec.ts:80 3000ms` / `:149 10_000ms` / `:206 1000ms`，`journey_01:66 5_000ms`、`journey_07:48/124 2_000ms` 等。**直接违反 §7.3"固定 sleep → 用 waitFor"**。 | Must Fix |
| **CSS 选择器（前端 e2e）** | 多数选择器用 `[data-activity-type]`、`[data-activity-status]`（语义属性，可接受）；少量 `input[type="password"]`（`ac_validation.spec.ts:103`）属语义化 CSS，临界但可接受。 | 良好（临界） |
| **不清理测试数据（前端 e2e）** | 14 个 e2e spec 中仅 4 个有 `beforeEach/afterEach/afterAll`（`feature_matrix`/`journey_02_task`/`journey_04_config`/`journey_05_triggers`）；**其余 10 个无任何清理钩子**，违反 §7.3"测试后清理数据"。 | Should Fix |
| **吞失败**（e2e 隐式通过） | `frontend/e2e/specs/ac_validation.spec.ts:106-110`：`try { waitForToolCard... } catch { console.log('未检测到工具卡片') }`——工具卡片没出现也绿。属"测试无断言"变种。 | Must Fix |
| **顺序依赖测试** | 抽样未发现明显顺序依赖；conftest 普遍使用 `setup_method`/`beforeEach` 隔离。 | 良好 |
| **大量 mock 不验证调用** | mock 普遍配 `assert_called*`，无"裸 mock"。 | 良好 |

---

## ⑥ 意图测试（Intent Testing / WHY）

**优秀样例（达标）**：
- `tests/test_track_cost_update_event.py` 顶部 docstring 明确编码 WHY：钉死 `cost_update` 事件契约（payload 必含 `pipeline_id` / token 必须单轮值 / tool_execute 轮不推送），并附根因（"原实现推送跨轮累计 total_tokens，且不带 pipeline_id → 前端进度条恒为 0"）。这是规则 §8 的标杆。
- `frontend/src/stores/__tests__/appendMessagesClientIdDedup.test.ts` 注释说明"修复前 appendMessages 只按 id 去重，乐观 user 与 API user clientMessageId 相同但 id 不同 → 切会话回来两条并存"，并断言可观察 messages 数组。
- `kernel/crates/api/tests/actions_execute_test.rs` 模块注释闭合前端链路契约（未知 cmd→404 / 已声明 cmd→200 / 缺字段→400）。

**反面样例（未达标）**：
- `kernel/crates/integration-tests/tests/bench_baseline.rs` 只 `println!` 平均耗时，**不断言任何性能不变量**——不回答"这次改动是否破坏性能契约"，仅为产出数字。详见 ⑦。
- 多数 e2e `journey_*.spec.ts` 描述用户旅程但断言较弱（`textContent.length > 0` 级别），WHY 表达不足。

---

## ⑦ CI 门禁断言（§9.5 契约 vs 噪声 / §9.4 timing 独立阻塞）

### 7.1 §9.4 关键不变量 timing 门禁——**全项目缺位（Must Fix）**

- 全仓库 **`@pytest.mark.timing` 标记数 = 0**（`grep -rc "@pytest.mark.timing" tests/ plugins/` 命中 0）。
- CI 无任何 timing stage（`.github/workflows/ci.yml` 中无 `timing` / `pytest -m timing` / 独立 stage）。
- 等价物：Rust 侧 `bench_baseline.rs` 用真实 `Instant::now()` 测平均，但**只打印不 assert**，且未标注为 timing 不变量；前端 `ac_validation.spec.ts:149 AC-8` 是事实上的 timing 测试（"登录后 10 秒仍在线"= Token TTL 不变量），却用 `waitForTimeout(10_000)` 真实墙钟固定等待——**既不注入可控时钟，也断言不了"边界"**（10s 通过只能证明 TTL≥10s 的一个点，无法证明退避/超时边界）。

→ **直接违反 §9.4"CI 必须包含独立 timing stage + @pytest.mark.timing 用例 + 阻断合并"**。问题清单 #3、#6、#9。

### 7.2 §9.5 CI 门禁断言"契约 vs 噪声"——**Python/Rust 合规，前端 e2e 有噪声**

- Python：未发现 `mock.call_count == N` 进入 CI 门禁（assert_called_once 多在外部 mock 边界）。
- Rust：`actions_execute_test.rs` 等纯断言 HTTP status/body 契约——**§9.5 标杆**。
- 前端：`ConversationNavigateTab.test.tsx:97-114` 在有 DOM 可观察的情况下断言 `navigateToTab.mock.calls[0][1]`，属"内部调用次数/参数"噪声（§9.5 反例），重构一次路由即误报（Nit）。

### 7.3 白盒诊断测试混入常规 CI——**轻度**

- `tests/suites/core/test_pipeline_stability.py`、`tests/suites/llm/test_keypool_selection.py` 含 `asyncio.sleep(0.05)` 模拟并发持有信号量——若进 CI 属"用固定延迟验证时序"的白盒诊断，应按 §9.5 分轨管理（移入非门禁诊断套件或改造为 fake clock + 不变量断言）。但因 `tests/suites/{core,llm}` 当前根本不进 CI（见 ⑧），实际未造成门禁噪声，仅是设计缺陷。

---

## ⑧ CI 实际覆盖核对（不假设，逐 job 实测）

### 8.1 `.github/workflows/ci.yml` job 全景

| Job | 触发 | working-directory | 实跑命令 | 实测覆盖范围 |
|-----|------|------|---------|--------------|
| `rust-lint` | push/PR | `kernel` | `cargo fmt --check` + `cargo clippy -- -D warnings` | Rust 全量 |
| `rust-build` | push/PR | `kernel` | `cargo build {debug,release}` | Rust 全量 |
| `rust-test` | push/PR | `kernel` | `cargo test --all` + 基线锁脚本 | **Rust 全量（含 src 内嵌 #[cfg(test)])** |
| `e2e-manual` | **仅 workflow_dispatch** | — | 只 `echo` 文档说明，**不跑任何 e2e** | **0 个 e2e 实跑** |
| `python-lint` | push/PR | `plugins/sdk` | `ruff check .` + `mypy plugins/sdk/src/agentos_plugin_sdk` | **仅 plugins/sdk** |
| `python-test` | push/PR | `plugins/sdk` | `pytest -v` | **仅 plugins/sdk（6 个 test 文件）** |
| `python-plugins-test` | push/PR | repo root | 列举 10 条路径（见下） | 见下表 |
| `tdd-gate` | push/PR | repo root | `scripts/check_tdd_compliance.py` | 静态合规检查 |

### 8.2 `python-plugins-test` 列举路径逐项核对

| CI 列举路径 | 实测存在？ | 实测 test 文件数 |
|-------------|-----------|-----------------|
| `plugins/test_system_plugins.py` | ✅ | 1 |
| `plugins/shared/system/llm/` | ✅ | 2 |
| `plugins/shared/system/tasks/` | ✅ | 1 |
| `plugins/shared/system/test_migration_batch3.py` | ✅ | 1 |
| `plugins/shared/tools/task/` | ✅ | 1 |
| `plugins/shared/tools/tests/` | ✅ | 1 |
| `plugins/shared/tools/builtin_tools/tests/` | ✅ | 1 |
| `tests/plugins/` | ✅ | 14 |
| `tests/suites/plugins/` | ✅ | 3 |
| `tests/suites/m6_plugins/` | ✅ 目录存在 | **0（空目录！）** |

`python-plugins-test` 实际跑到的 Python test 文件 ≈ **24 个**；加 `python-test` 的 6 个 SDK 文件，CI 实跑 Python ≈ **30 / 157 ≈ 19%**。

### 8.3 CI 覆盖实测总账（核心结论）

| 范围 | 文件总数 | CI 实跑 | 覆盖率 | 评级 |
|------|----------|---------|--------|------|
| Rust（kernel/crates） | 43 + 内嵌 | 全量 | **100%** | ✅ |
| Python（tests/ + plugins/） | 157 | ~30 | **~19%** | ❌ Must Fix |
| 前端 vitest（src） | 137 | **0** | **0%** | ❌ Must Fix |
| 前端 Playwright（e2e） | 14 | **0**（e2e-manual 仅 dispatch 且只 echo） | **0%** | ❌ Must Fix |
| timing 不变量门禁 | 应有 | **0** | 0% | ❌ Must Fix |

**未进 CI 的关键测试**（高风险盲区）：
- `tests/test_p0_regression.py`、`tests/test_isolation_*.py`（10 个）、`tests/test_security_check_*.py`（5 个）、`tests/test_host_mode_security.py`、`tests/test_sensitive_paths.py`、`tests/test_llm_timeout_protection.py`、`tests/test_track_*.py`（4 个）、`tests/test_new_project_e2e.py` —— **安全/隔离/P0 回归完全不进 CI**。
- `tests/suites/{core,llm,task,agent,cli,config,memory,pipeline,stage,tools,websocket}` —— 整套按域组织的 suite 不进 CI。
- `tests/e2e/`（6）、`tests/e2e_02/`（5）—— Python e2e 完全不进 CI。
- 前端 137 个组件测试、14 个 e2e —— **零进 CI**。

**额外**：`pytest tests/ --collect-only` 报 1 个 collection error（`tests/integration - ModuleNotFoundError: No module named 'pipeline'`），即便想本地全跑也会中断。

---

## ⑨ 问题清单

格式：`path:line | 级别 | 维度 | 问题 | 修复`

| # | 位置 | 级别 | 维度 | 问题 | 修复建议 |
|---|------|------|------|------|----------|
| 1 | `.github/workflows/ci.yml:整个文件` | **Must Fix** | CI 门禁 | 无 vitest job，前端 137 个组件测试 **0% 进 CI** | 新增 `frontend-test` job：`cd frontend && npm ci && npx vitest --run && npm run typecheck` |
| 2 | `.github/workflows/ci.yml:整个文件` | **Must Fix** | CI 门禁 | 无 playwright job，14 个 e2e spec **0% 进 CI**；`e2e-manual` job `if: github.event_name=='workflow_dispatch'` 且 step 只 `echo` 文档，**任何 PR 都不会跑 e2e** | 新增 `frontend-e2e` job（启动服务后跑零外部依赖的 e2e 子集），把 `e2e-manual` 文档说明合并进来；至少 journey 冒烟进 PR 级 CI |
| 3 | `.github/workflows/ci.yml:整个文件` + `tests/` + `plugins/` | **Must Fix** | §9.4 timing 门禁 | CI 无独立 timing stage，全仓库 `@pytest.mark.timing` 标记 = 0；§9.4 明确要求 timing stage 阻断合并 | 标记关键时序不变量用例（如 token TTL、重试退避下限、事件顺序）为 `@pytest.mark.timing`，新增独立 timing stage 与 unit 隔离运行 |
| 4 | `.github/workflows/ci.yml:python-test job` | **Must Fix** | CI 覆盖 | `working-directory: plugins/sdk` + `pytest -v`，仅覆盖 SDK 6 个 test；`tests/` 顶层 64 个 test_*.py（含 `test_p0_regression` / `test_isolation_*` / `test_security_check_*` / `test_host_mode_security` / `test_llm_timeout_protection` / `test_track_*`）**全部不进 CI** | 扩大 pytest 命令为 `pytest tests/ plugins/` 或新增 `python-core-test` job；先修复 `tests/integration` 的 collection error |
| 5 | `.github/workflows/ci.yml:e2e-manual job` | **Must Fix** | CI 门禁（语义造假） | job 注释自称"热加载/用户旅程实测"，但 step 仅 `echo "本 job 仅在手动触发时运行"`，**从未真正执行 e2e**——给"有 e2e 覆盖"的假象 | 要么删除该 job 避免误导，要么落地为真正运行零 LLM 依赖的冒烟 e2e |
| 6 | `frontend/e2e/specs/ac_validation.spec.ts:149` | **Must Fix** | §6.4/§9.4 timing + §7.3 固定 sleep | AC-8"登录后 10 秒仍在线"是事实上的 Token TTL 时序不变量测试，却用 `page.waitForTimeout(10_000)` 真实墙钟固定等待——既不注入可控时钟，也无法断言 TTL 边界 | 用 Playwright `page.clock`/`vi.useFakeTimers` 注入可控时钟，断言"TTL 边界前后仍/已登出"；或在前端单测中验证 token 过期逻辑 |
| 7 | `frontend/e2e/specs/ac_validation.spec.ts:106-110` | **Must Fix** | 无断言/吞失败（§7） | `try { const toolCard = await waitForToolCard(...) } catch { console.log('未检测到工具卡片') }`——工具卡片未出现测试仍绿，等同无断言 | 去掉 try/catch 或在 catch 内 `expect.fail(...)`；让失败显式 |
| 8 | `frontend/e2e/specs/*.spec.ts`（`journey_03_tools`/`journey_05_triggers`/`journey_07_auth`/`ac_validation` 等 13 文件） | **Should Fix** | §7.3 固定 sleep | 全 e2e 共 50+ 处 `waitForTimeout(300~10_000)` | 改用 `await expect(locator).toBe...()` / `page.waitForFunction(...)` / `waitFor` 等条件等待 |
| 9 | `kernel/crates/integration-tests/tests/bench_baseline.rs` | **Should Fix** | §6.4/§9.4 timing 不变量 | 用真实 `Instant::now()` 测平均耗时但只 `println!` 不 `assert`，是 benchmark 不是 timing 门禁；未标注为 timing 不变量 | 加性能上限断言（如 `assert!(avg < Duration::from_millis(X))`），或显式标记为非门禁基准；避免依赖真实墙钟绝对值 |
| 10 | `tests/channels/test_channel_gateway.py:54-66, 92` | **Should Fix** | §9.5 impl-detail | `mock_adapter.start.assert_called_once()` / `handler.assert_called_once()` 断言内部协作者被调几次——重构 ChannelGateway 内部调度即误报 | 改断言可观察行为：adapter 进入 started 状态 / gateway 暴露的 `started` 事件 / 消息流转结果 |
| 11 | `tests/integration/`（整个目录） | **Should Fix** | 金字塔中层 + 误导 | 仅 conftest + __init__，0 个 test 文件，但目录存在易让人误以为集成层已覆盖；且 collection 报错 `No module named 'pipeline'` | 删除空目录或在 pytest config 显式 ignore；若需集成层，补齐真实集成测试 |
| 12 | `.github/workflows/ci.yml:python-plugins-test` | **Should Fix** | CI 引用失真 | 引用 `tests/suites/m6_plugins/` 但该目录 0 个 test 文件（空目录），命令空跑 | 清理引用，或补齐 m6_plugins 实质测试 |
| 13 | `frontend/src/components/chat/__tests__/ConversationNavigateTab.test.tsx:97-114` | **Nit** | §9.5 impl-detail | `expect(mocks.navigateToTab.mock.calls[0][1]).toBe('pipe-xyz')` 断言内部 navigate 调用参数，在有可观察 DOM/URL 的情况下属噪声 | 优先断言 active tab 高亮 / URL hash 等可观察结果 |
| 14 | `frontend/e2e/specs/`（10/14 文件） | **Should Fix** | §7.3 不清理 | 多数 e2e spec 无 `beforeEach/afterEach/afterAll`，未清理 localStorage / 会话 / 后端数据 | 加 `afterEach` 清理 localStorage、登出、重置后端 fixture |
| 15 | `tests/suites/core/test_pipeline_stability.py:77`、`tests/suites/llm/test_keypool_selection.py:68,114` | **Should Fix** | §6.4 timing + §9.5 分轨 | 用 `asyncio.sleep(0.05)` 固定延迟模拟并发时序，是时序测试却未标 `@pytest.mark.timing`，且未注入 fake clock | 改 fake clock + 不变量断言（如"信号量持有期间并发请求数 ≤ 上限"），并按 §9.4 标记 timing |
| 16 | `kernel/crates/{invoker,tenant,native-sdk,hooks}` | **Should Fix** | Rust 测试覆盖盲区 | 4 个 crate 无独立 `tests/` 目录（仅 src 内嵌 cfg(test)）；`native-sdk-test-plugin` 与 `hooks` 连 src 内嵌测试都没有 | 为核心 crate（尤其 `invoker`——CI 注释自称其单测覆盖热加载）补 `tests/` 集成测试；`hooks` 至少补 src 内嵌 |

---

## ⑩ 统计与结论

### 10.1 问题统计

| 级别 | 数量 | 主要分布 |
|------|------|----------|
| **Must Fix** | **7** | CI 门禁（#1/#2/#4/#5）、timing 门禁缺位（#3）、e2e 固定 sleep + 吞失败（#6/#7） |
| Should Fix | 8 | e2e sleep/清理（#8/#14）、bench 无断言（#9）、impl-detail（#10）、空目录（#11/#12）、timing 设计（#15）、Rust crate 覆盖（#16） |
| Nit | 1 | 前端 impl-detail（#13） |
| **合计** | 16 | — |

### 10.2 金字塔占比

- 实测结构估算：**单元 ~50% / 集成 ~43% / E2E ~7%**（目标 70/20/10）。
- 中部集成层偏厚（Rust 端到端 router 测试 + Python 顶层回归测试），单元层偏薄。
- 但**比例不是首要矛盾**——见下。

### 10.3 CI 覆盖实测（最关键结论）

- **Rust 100% 覆盖**（`cargo test --all` 真跑全量 + src 内嵌）。
- **Python 仅 ~19% 覆盖**（~30/157），且 P0 回归 / 隔离 / 安全检查 / 时序保护等关键 suite 全不进 CI。
- **前端 0% 覆盖**（vitest 无 job、playwright 无 job；`e2e-manual` 仅 workflow_dispatch 且只 echo）。
- **timing 门禁 0%**（0 个 `@pytest.mark.timing`，无独立 timing stage）——**直接违反规则 §9.4**。

### 10.4 Top 5 优先修复

1. **【#1/#2】前端 0 进 CI**：补 vitest job + playwright job，至少 journey 冒烟进 PR 级 CI。
2. **【#4】Python tests/ 顶层不进 CI**：扩大 `python-test` 范围或新增 job，让 `test_p0_regression` / `test_isolation_*` / `test_security_check_*` / `test_track_*` 进 CI；先修 `tests/integration` collection error。
3. **【#3/#6/#15】timing 门禁全缺位**：标记关键时序用例 + 新增独立 timing stage（§9.4）；AC-8 改可控时钟 + 不变量断言（§6.4）。
4. **【#5/#12】CI 语义造假清理**：`e2e-manual` 落地为真跑或删除；`tests/suites/m6_plugins/` 空引用清理。
5. **【#7】e2e 吞失败**：`ac_validation.spec.ts:106-110` try/catch 改为显式失败，避免"绿但实际未验证"。

### 10.5 整体结论

- **审查结论：Request Changes**（存在 7 个 Must Fix）。
- 测试**编写质量**整体良好（意图注释到位、Rust 行为测试范式标杆、前端组件测试重行为轻实现、命名规范、无断言测试近乎为零）；**问题不在"测试怎么写"，而在"CI 跑不跑"**。
- 最大系统性风险：**81% 的 Python 测试与 100% 的前端测试不进 CI**，再加上 timing 门禁完全缺位——大量测试仅本地可跑、PR 门禁不感知，规则 §9.2"任何阶段失败阻塞合并"在 Python/前端维度形同虚设。
- 修复优先级：**先打通 CI 覆盖（#1/#2/#4），再补 timing 门禁（#3），最后收尾 e2e 卫生（#6/#7/#8/#14）**。

---

*报告路径：`reports/audit_round3/T5_tests.md`。本报告为独立审查产物，所有结论以代码现状与规则文本为唯一依据。*
