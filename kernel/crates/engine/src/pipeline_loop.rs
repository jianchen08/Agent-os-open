//! # 配置驱动的管道执行器（Pipeline Executor）
//!
//! 0.2 引擎的"解释执行器"职责实现（ADR ⑥）：读 `PipelineConfig`，按 `steps` 顺序执行，
//! 据 `loop` / `routes` 决定循环与分支。一套执行器 + 不同 YAML = 不同行为。
//!
//! ## 三级命中规则（[来源: 任务 §execute_step_inner]）
//!
//! 对 `step.steps` 里的每一项，按如下顺序解析：
//! ① 当前管道 step id（`config.find_step`）→ 组合节点递归执行
//! ② 公共 step 库（`step_library.find`）→ 组合节点递归执行
//! ③ 插件名（在已知插件集合中）→ 通过 `PluginInvoker` 调用原子插件
//!
//! 三级都 miss：记 `error!` 但不 panic（error_policy 简化为记录后继续）。
//!
//! ## 状态流转
//!
//! `state` 是 `serde_json::Value`（Object）。两个特殊 key 控制流程：
//! - `ended` = true：当前轮（及外层 loop）立即终止
//! - `suspended` = true：当前轮立即挂起
//!
//! [来源: docs/tasks/task_06_pipeline_engine.md]
//! [来源: docs/working/adr_engine_design.md]

use std::collections::HashSet;
use std::path::PathBuf;
use std::sync::Arc;

use tracing::{error, warn};

use agentos_core::traits::{PluginInvoker, StorageBackend};
use agentos_core::types::{
    ContentLoader, EngineError, PipelineConfig, PipelineStep, PluginContext, PluginError,
    PluginResult, Route, RouteNext, StepLibrary, TenantContext,
};

use crate::condition::eval_condition;
use crate::template::{render_template, render_value};

/// 配置驱动的管道执行器。
///
/// 持有：
/// - `invoker`：调用原子插件（命中规则③）
/// - `project_root`：`{{path:...}}` 模板解析基准
/// - `default_tenant`：构造 `PluginContext` 时注入的租户上下文
/// - `plugin_ids`：已知插件 id 集合，`lookup_plugin` 在里面查（命中规则③判定）
/// - `store` / `run_id` / `branch_id`：构造 `ContentLoader`（ADR ⑦）
pub struct PipelineExecutor {
    invoker: Arc<dyn PluginInvoker>,
    project_root: PathBuf,
    default_tenant: TenantContext,
    plugin_ids: HashSet<String>,
    store: Arc<dyn StorageBackend>,
    run_id: String,
    branch_id: String,
}

impl PipelineExecutor {
    /// 构造执行器。
    ///
    /// # Arguments
    /// * `invoker` - 插件调用器（命中规则③用）
    /// * `project_root` - 模板 `{{path:...}}` 解析基准
    /// * `default_tenant` - 默认租户上下文
    /// * `plugin_ids` - 已知插件 id 列表（从 manifest 加载）
    /// * `store` - 存储后端，用于构造 `ContentLoader`
    /// * `run_id` / `branch_id` - 当前运行实例 / 分支标识
    pub fn new(
        invoker: Arc<dyn PluginInvoker>,
        project_root: PathBuf,
        default_tenant: TenantContext,
        plugin_ids: impl IntoIterator<Item = String>,
        store: Arc<dyn StorageBackend>,
        run_id: impl Into<String>,
        branch_id: impl Into<String>,
    ) -> Self {
        Self {
            invoker,
            project_root,
            default_tenant,
            plugin_ids: plugin_ids.into_iter().collect(),
            store,
            run_id: run_id.into(),
            branch_id: branch_id.into(),
        }
    }

    /// 执行管道。
    ///
    /// `initial_state` 是初始状态（含 `message` / `agent_id` 等）。
    /// 返回最终 state。
    ///
    /// 流程（[来源: 任务 §run 逻辑]）：
    /// 1. state 默认设 `"ended" = false`（如未设）
    /// 2. 如果 `config.loop_config.enabled`：while not ended，循环执行 steps，
    ///    受 `max_iterations` 安全阀约束（>0 时生效；-1=无限）
    /// 3. 否则单次执行 steps
    pub async fn run(
        &self,
        config: &PipelineConfig,
        step_library: &StepLibrary,
        initial_state: serde_json::Value,
    ) -> Result<serde_json::Value, EngineError> {
        let mut state = initial_state;
        ensure_object(&mut state);
        // 默认设 ended=false（如未设）
        if !key_present(&state, "ended") {
            set_key(&mut state, "ended", serde_json::Value::Bool(false));
        }

        if config.loop_config.enabled {
            let max_iters = config.loop_config.max_iterations;
            let mut iteration: i32 = 0;
            loop {
                if truthy_flag(&state, "ended") {
                    break;
                }
                iteration += 1;
                if max_iters > 0 && iteration > max_iters {
                    break;
                }
                self.execute_steps(&config.steps, &mut state, config, step_library)
                    .await;
                if truthy_flag(&state, "ended") {
                    break;
                }
            }
        } else {
            self.execute_steps(&config.steps, &mut state, config, step_library)
                .await;
        }

        Ok(state)
    }

    /// 遍历 step 列表，遇到 `ended` / `suspended` 即停。
    async fn execute_steps(
        &self,
        steps: &[PipelineStep],
        state: &mut serde_json::Value,
        config: &PipelineConfig,
        step_library: &StepLibrary,
    ) {
        for step in steps {
            if truthy_flag(state, "ended") || truthy_flag(state, "suspended") {
                break;
            }
            self.execute_step(step, state, config, step_library).await;
        }
    }

    /// 执行单个 step：context 注入 →（可选）step 自带循环 → 三级命中执行 → 路由。
    ///
    /// 注意：step 自带循环时，路由放在循环体内由 `execute_step_inner` 后应用，
    /// 且当前实现把路由统一放在非循环分支末尾执行（循环体内每轮末尾也会执行路由，
    /// 这样能及时 break 出循环）。详见 [来源: 任务 §execute_step] 的实现取舍。
    ///
    /// 由于 `execute_step` 与 `execute_step_inner` 相互递归调用（命中规则①②的
    /// 组合节点会递归），Rust async fn 无法直接表达无限大小的 future，
    /// 这里用 `Box::pin` 引入间接层（boxed future）打破无限大小。
    fn execute_step<'a>(
        &'a self,
        step: &'a PipelineStep,
        state: &'a mut serde_json::Value,
        config: &'a PipelineConfig,
        step_library: &'a StepLibrary,
    ) -> std::pin::Pin<Box<dyn std::future::Future<Output = ()> + Send + 'a>> {
        Box::pin(self.execute_step_impl(step, state, config, step_library))
    }

    async fn execute_step_impl(
        &self,
        step: &PipelineStep,
        state: &mut serde_json::Value,
        config: &PipelineConfig,
        step_library: &StepLibrary,
    ) {
        // 1. context 注入：渲染 step.context 模板，merge 进 state
        let rendered = render_value(
            &serde_json::to_value(&step.context).unwrap_or(serde_json::Value::Object(Default::default())),
            state,
            &self.project_root,
        );
        if let Some(obj) = rendered.as_object() {
            for (k, v) in obj {
                set_key(state, k, v.clone());
            }
        }

        // 2. step 自带 loop_config：循环执行 execute_step_inner
        if let Some(loop_cfg) = &step.loop_config {
            if loop_cfg.enabled {
                let max_iters = loop_cfg.max_iterations;
                let mut i: i32 = 0;
                loop {
                    if truthy_flag(state, "ended") || truthy_flag(state, "suspended") {
                        break;
                    }
                    i += 1;
                    if max_iters > 0 && i > max_iters {
                        break;
                    }
                    self.execute_step_inner(step, state, config, step_library)
                        .await;
                    // 循环体里也应用路由（及时结束/挂起）
                    if !step.routes.is_empty() {
                        apply_routes(&step.routes, state);
                    }
                    if truthy_flag(state, "ended") || truthy_flag(state, "suspended") {
                        break;
                    }
                }
                return;
            }
        }

        // 3. 非循环：直接执行
        self.execute_step_inner(step, state, config, step_library)
            .await;

        // 4. 路由处理
        if !step.routes.is_empty() {
            apply_routes(&step.routes, state);
        }
    }

    /// 三级命中执行 `step.steps` 列表。
    async fn execute_step_inner(
        &self,
        step: &PipelineStep,
        state: &mut serde_json::Value,
        config: &PipelineConfig,
        step_library: &StepLibrary,
    ) {
        for item in &step.steps {
            if truthy_flag(state, "ended") || truthy_flag(state, "suspended") {
                break;
            }
            // 动态插件名：先渲染模板（处理 {{state.core_plugin}} 这类）
            let resolved = render_template(item, state, &self.project_root);
            let item = resolved.as_str();

            // 命中①当前管道 step id
            if let Some(target) = config.find_step(item) {
                let target = target.clone();
                self.execute_step(&target, state, config, step_library).await;
                continue;
            }
            // 命中②公共 step 库
            if let Some(target) = step_library.find(item) {
                let target = target.clone();
                self.execute_step(&target, state, config, step_library).await;
                continue;
            }
            // 命中③插件名
            if self.lookup_plugin(item) {
                match self.invoke_plugin(item, state.clone()).await {
                    Ok(result) => {
                        if result.error.is_none() {
                            // merge state_updates
                            for (k, v) in &result.state_updates {
                                set_key(state, k, v.clone());
                            }
                            if result.skip_remaining {
                                break;
                            }
                        } else {
                            // error_policy 简化：warn + 继续
                            warn!(
                                plugin = %item,
                                error = ?result.error,
                                "plugin returned error, continuing (error_policy=skip)"
                            );
                        }
                    }
                    Err(e) => {
                        // invoker 自身报错（如 sidecar 不可达）：warn + 继续
                        warn!(
                            plugin = %item,
                            error = %e,
                            "plugin invoker error, continuing"
                        );
                    }
                }
                continue;
            }
            // 三级都 miss
            error!(
                step_or_plugin = %item,
                "step/plugin '{}' 未找到，请下载或安装（已记录，继续后续步骤）",
                item
            );
            // 记录错误但继续（不 panic）
        }
    }

    /// 判定插件是否存在于已知插件集合（命中规则③）。
    fn lookup_plugin(&self, plugin_id: &str) -> bool {
        self.plugin_ids.contains(plugin_id)
    }

    /// 构造 `PluginContext` 并调用 `invoker`。
    async fn invoke_plugin(
        &self,
        plugin_id: &str,
        state: serde_json::Value,
    ) -> Result<PluginResult, PluginError> {
        let content_loader = ContentLoader::new(
            Arc::clone(&self.store),
            self.run_id.clone(),
            self.branch_id.clone(),
            0,
        );
        let ctx = PluginContext::new(
            state,
            serde_json::Value::Object(Default::default()),
            self.default_tenant.clone(),
            uuid::Uuid::nil(),
            content_loader,
        );
        self.invoker.invoke_pipeline_plugin(plugin_id, &ctx).await
    }
}

// ── 路由处理 ──────────────────────────────────────────────────

/// 应用路由分支：按 YAML 顺序匹配第一个 `when` 为真的分支，执行其 `then`。
///
/// 匹配后立即 `break`（priority 由 YAML 顺序体现）。
fn apply_routes(routes: &[Route], state: &mut serde_json::Value) {
    for route in routes {
        if eval_condition(&route.when, state) {
            // set 字段
            for (k, v) in &route.then.set {
                set_key(state, k, v.clone());
            }
            match &route.then.next {
                RouteNext::Loop => { /* 继续，外层 while 会循环 */ }
                RouteNext::End => {
                    set_key(state, "ended", serde_json::Value::Bool(true));
                }
                RouteNext::Wait => {
                    set_key(state, "suspended", serde_json::Value::Bool(true));
                }
                RouteNext::Step(id) => {
                    // 简化：记到 state.next_step（不真正跳转）
                    set_key(state, "next_step", serde_json::Value::String(id.clone()));
                }
            }
            break;
        }
    }
}

// ── state 操作工具 ─────────────────────────────────────────────

/// 确保 state 是 Object（非 Object 时替换为空 Object）。
fn ensure_object(state: &mut serde_json::Value) {
    if !state.is_object() {
        *state = serde_json::Value::Object(serde_json::Map::new());
    }
}

/// state 是否含指定 key。
fn key_present(state: &serde_json::Value, key: &str) -> bool {
    state.as_object().map(|o| o.contains_key(key)).unwrap_or(false)
}

/// 设置 state[key] = value（state 必须是 Object，否则忽略）。
fn set_key(state: &mut serde_json::Value, key: &str, value: serde_json::Value) {
    if let Some(obj) = state.as_object_mut() {
        obj.insert(key.to_string(), value);
    }
}

/// 读取 state[key] 的布尔值（缺失/非 bool 返回 false）。
fn truthy_flag(state: &serde_json::Value, key: &str) -> bool {
    state
        .as_object()
        .and_then(|o| o.get(key))
        .and_then(|v| v.as_bool())
        .unwrap_or(false)
}

// ═════════════════════════════════════════════════════════════════
// 单元测试
// ═════════════════════════════════════════════════════════════════

#[cfg(test)]
mod tests {
    use super::*;
    use async_trait::async_trait;
    use serde_json::json;
    use std::collections::HashMap;
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::sync::Mutex;

    use agentos_core::traits::StorageBackend;
    use agentos_core::types::{
        Branch, Message, MessageRecord, RunRecord, RunStatus, ToolExecutionResult, TraceEntry,
    };

    // ── 测试基础设施 ──────────────────────────────────────────

    /// 可编程的 MockInvoker：按 plugin_id 返回预设的 PluginResult。
    /// 同时统计每个插件被调用的次数。
    struct MockInvoker {
        results: Mutex<HashMap<String, PluginResult>>,
        calls: Mutex<HashMap<String, usize>>,
    }

    impl MockInvoker {
        fn new() -> Self {
            Self {
                results: Mutex::new(HashMap::new()),
                calls: Mutex::new(HashMap::new()),
            }
        }

        fn set_result(&self, plugin_id: &str, result: PluginResult) {
            self.results
                .lock()
                .unwrap()
                .insert(plugin_id.to_string(), result);
        }

        fn call_count(&self, plugin_id: &str) -> usize {
            *self
                .calls
                .lock()
                .unwrap()
                .get(plugin_id)
                .unwrap_or(&0)
        }
    }

    #[async_trait]
    impl PluginInvoker for MockInvoker {
        async fn invoke_pipeline_plugin(
            &self,
            plugin_id: &str,
            _ctx: &PluginContext,
        ) -> Result<PluginResult, PluginError> {
            // 计数
            *self
                .calls
                .lock()
                .unwrap()
                .entry(plugin_id.to_string())
                .or_insert(0) += 1;
            // 返回预设结果（缺失则返回空成功）
            let result = self
                .results
                .lock()
                .unwrap()
                .get(plugin_id)
                .cloned()
                .unwrap_or_default();
            Ok(result)
        }

        async fn invoke_tool(
            &self,
            _plugin_id: &str,
            _tool_name: &str,
            _inputs: &serde_json::Value,
        ) -> Result<ToolExecutionResult, PluginError> {
            Ok(ToolExecutionResult::success(serde_json::Value::Null))
        }

        async fn send_lifecycle_hook(
            &self,
            _plugin_id: &str,
            _hook: agentos_core::traits::LifecycleHook,
            _context: &agentos_core::traits::HookContext,
        ) -> Result<(), PluginError> {
            Ok(())
        }
    }

    /// 空 StorageBackend，仅用于构造 ContentLoader。
    struct NullStorage;
    #[async_trait]
    impl StorageBackend for NullStorage {
        async fn get_run(&self, _run_id: &str) -> Result<RunRecord, agentos_core::types::StorageError> {
            Err(agentos_core::types::StorageError::NotFound("null".into()))
        }
        async fn get_messages(
            &self,
            _run_id: &str,
            _branch_id: &str,
        ) -> Result<Vec<MessageRecord>, agentos_core::types::StorageError> {
            Ok(vec![])
        }
        async fn get_recent_messages(
            &self,
            _run_id: &str,
            _branch_id: &str,
            _n: usize,
        ) -> Result<Vec<Message>, agentos_core::types::StorageError> {
            Ok(vec![])
        }
        async fn get_blob(
            &self,
            _blob_id: &str,
        ) -> Result<Vec<u8>, agentos_core::types::StorageError> {
            Ok(vec![])
        }
        async fn append_trace(&self, _entry: TraceEntry) -> Result<(), agentos_core::types::StorageError> {
            Ok(())
        }
        async fn create_branch(&self, _branch: Branch) -> Result<(), agentos_core::types::StorageError> {
            Ok(())
        }
        async fn update_run_status(
            &self,
            _run_id: &str,
            _status: RunStatus,
            _branch: Option<&str>,
            _seq: Option<u32>,
        ) -> Result<(), agentos_core::types::StorageError> {
            Ok(())
        }
    }

    /// 测试夹具：构造一个 PipelineExecutor + MockInvoker（可拿引用设置结果）。
    struct Fixture {
        executor: PipelineExecutor,
        invoker: Arc<MockInvoker>,
    }

    impl Fixture {
        fn build(plugin_ids: &[&str]) -> Self {
            let invoker = Arc::new(MockInvoker::new());
            let store: Arc<dyn StorageBackend> = Arc::new(NullStorage);
            let executor = PipelineExecutor::new(
                invoker.clone() as Arc<dyn PluginInvoker>,
                PathBuf::from("."),
                TenantContext::new("tenant_test", "session_test"),
                plugin_ids.iter().map(|s| s.to_string()),
                store,
                "run_test",
                "main",
            );
            Self { executor, invoker }
        }

        async fn run(
            &self,
            config: &PipelineConfig,
            library: &StepLibrary,
            initial: serde_json::Value,
        ) -> serde_json::Value {
            self.executor
                .run(config, library, initial)
                .await
                .expect("run should succeed")
        }
    }

    /// 构造简单的 atomic step（引用一个插件名）。
    fn atomic_step(id: &str, plugin: &str) -> PipelineStep {
        PipelineStep {
            id: id.to_string(),
            steps: vec![plugin.to_string()],
            context: HashMap::new(),
            routes: vec![],
            loop_config: None,
        }
    }

    /// state_updates 辅助构造。
    fn updates(pairs: &[(&str, serde_json::Value)]) -> HashMap<String, serde_json::Value> {
        pairs
            .iter()
            .map(|(k, v)| (k.to_string(), v.clone()))
            .collect()
    }

    /// 计数式 invoker：每次调用计数 +1，可选在第 N 次后 set `ended` 或 `suspended`。
    /// 用于验证循环 / 挂起逻辑。
    struct CountingInvoker {
        counter: Arc<AtomicUsize>,
        /// 第 N 次调用后 set ended=true（0=永不）。stop_after=3 表示第 3 次起 set ended。
        stop_after: usize,
        /// 第 N 次调用后 set suspended=true（0=永不）。优先级低于 stop_after。
        set_suspended_after: usize,
    }

    #[async_trait]
    impl PluginInvoker for CountingInvoker {
        async fn invoke_pipeline_plugin(
            &self,
            _plugin_id: &str,
            _ctx: &PluginContext,
        ) -> Result<PluginResult, PluginError> {
            let n = self.counter.fetch_add(1, Ordering::SeqCst) + 1;
            let mut updates = HashMap::new();
            updates.insert("round".into(), json!(n));
            if self.stop_after > 0 && n >= self.stop_after {
                updates.insert("ended".into(), json!(true));
            } else if self.set_suspended_after > 0 && n >= self.set_suspended_after {
                updates.insert("suspended".into(), json!(true));
            }
            Ok(PluginResult {
                state_updates: updates,
                ..Default::default()
            })
        }
        async fn invoke_tool(
            &self,
            _p: &str,
            _t: &str,
            _i: &serde_json::Value,
        ) -> Result<ToolExecutionResult, PluginError> {
            Ok(ToolExecutionResult::success(serde_json::Value::Null))
        }
        async fn send_lifecycle_hook(
            &self,
            _p: &str,
            _h: agentos_core::traits::LifecycleHook,
            _c: &agentos_core::traits::HookContext,
        ) -> Result<(), PluginError> {
            Ok(())
        }
    }

    /// 用给定 invoker + plugin_ids 构造一个 PipelineExecutor（NullStorage 后端）。
    fn make_executor(invoker: Arc<dyn PluginInvoker>, plugin_ids: &[&str]) -> PipelineExecutor {
        let store: Arc<dyn StorageBackend> = Arc::new(NullStorage);
        PipelineExecutor::new(
            invoker,
            PathBuf::from("."),
            TenantContext::new("t", "s"),
            plugin_ids.iter().map(|s| s.to_string()),
            store,
            "r",
            "b",
        )
    }

    // ── 测试用例 ──────────────────────────────────────────────

    #[tokio::test]
    async fn test_single_step_atomic() {
        // 一个 step 引用原子插件；验证插件被调用 + state_updates 被 merge
        let fixture = Fixture::build(&["echo"]);
        fixture.invoker.set_result(
            "echo",
            PluginResult {
                state_updates: updates(&[("reply", json!("hello"))]),
                ..Default::default()
            },
        );
        let config = PipelineConfig {
            name: "single".into(),
            loop_config: Default::default(),
            steps: vec![atomic_step("s1", "echo")],
        };
        let library = StepLibrary::default();
        let final_state = fixture.run(&config, &library, json!({})).await;
        assert_eq!(fixture.invoker.call_count("echo"), 1);
        assert_eq!(final_state["reply"], json!("hello"));
        // ended 默认 false
        assert_eq!(final_state["ended"], json!(false));
    }

    #[tokio::test]
    async fn test_composite_step() {
        // 一个组合 step 引用两个子 step（命中规则①：当前管道 step id），验证递归执行。
        //
        // 注意 spec 语义：execute_steps 会遍历 config.steps 中【所有】step，
        // 而 find_step 也在 config.steps 中查。如果 child_a/child_b 同时在 config.steps 里，
        // 它们会被顶层 execute_steps 跑一次，又被 parent 的递归引用跑一次（共 2 次）。
        // 为了精确验证"递归执行"（而不是重复触发），这里只把 parent 放进 config.steps，
        // 子 step 放进公共 step 库——它们只能通过 parent 的引用被命中（命中规则②）。
        let fixture = Fixture::build(&["a", "b"]);
        fixture.invoker.set_result(
            "a",
            PluginResult {
                state_updates: updates(&[("a_val", json!(1))]),
                ..Default::default()
            },
        );
        fixture.invoker.set_result(
            "b",
            PluginResult {
                state_updates: updates(&[("b_val", json!(2))]),
                ..Default::default()
            },
        );
        let config = PipelineConfig {
            name: "composite".into(),
            loop_config: Default::default(),
            steps: vec![PipelineStep {
                id: "parent".into(),
                steps: vec!["child_a".into(), "child_b".into()],
                context: HashMap::new(),
                routes: vec![],
                loop_config: None,
            }],
        };
        let mut library = StepLibrary::default();
        library
            .steps
            .insert("child_a".to_string(), atomic_step("child_a", "a"));
        library
            .steps
            .insert("child_b".to_string(), atomic_step("child_b", "b"));
        let final_state = fixture.run(&config, &library, json!({})).await;
        assert_eq!(fixture.invoker.call_count("a"), 1);
        assert_eq!(fixture.invoker.call_count("b"), 1);
        assert_eq!(final_state["a_val"], json!(1));
        assert_eq!(final_state["b_val"], json!(2));
    }

    #[tokio::test]
    async fn test_composite_step_double_trigger_semantics() {
        // 补充验证 spec 语义：如果子 step 同时在 config.steps 里，会被执行 2 次
        // （顶层 execute_steps 一次 + parent 递归一次）。这验证"重复触发"是 spec 的
        // 既定行为，而非 bug。
        let fixture = Fixture::build(&["a"]);
        fixture.invoker.set_result(
            "a",
            PluginResult {
                state_updates: updates(&[("a_val", json!(1))]),
                ..Default::default()
            },
        );
        let config = PipelineConfig {
            name: "double".into(),
            loop_config: Default::default(),
            steps: vec![
                PipelineStep {
                    id: "parent".into(),
                    steps: vec!["child".into()],
                    context: HashMap::new(),
                    routes: vec![],
                    loop_config: None,
                },
                atomic_step("child", "a"),
            ],
        };
        let _ = fixture
            .run(&config, &StepLibrary::default(), json!({}))
            .await;
        // 顶层执行 parent（递归触发 child 1 次）+ 顶层执行 child（1 次）= 2 次
        assert_eq!(fixture.invoker.call_count("a"), 2);
    }

    #[tokio::test]
    async fn test_step_library() {
        // step 引用公共 step 库的 id（命中规则②）
        let fixture = Fixture::build(&["lib_plugin"]);
        fixture.invoker.set_result(
            "lib_plugin",
            PluginResult {
                state_updates: updates(&[("lib_out", json!("from_library"))]),
                ..Default::default()
            },
        );
        let config = PipelineConfig {
            name: "with_lib".into(),
            loop_config: Default::default(),
            steps: vec![atomic_step("caller", "shared_step")],
        };
        let mut library = StepLibrary::default();
        library
            .steps
            .insert("shared_step".to_string(), atomic_step("shared_step", "lib_plugin"));
        let final_state = fixture.run(&config, &library, json!({})).await;
        assert_eq!(fixture.invoker.call_count("lib_plugin"), 1);
        assert_eq!(final_state["lib_out"], json!("from_library"));
    }

    #[tokio::test]
    async fn test_loop() {
        // loop.enabled=true，插件每次递增计数器；第 3 次 set ended=true 后停止
        let counter = Arc::new(AtomicUsize::new(0));
        let executor = make_executor(
            Arc::new(CountingInvoker {
                counter: counter.clone(),
                stop_after: 3,
                set_suspended_after: 0, // 不挂起
            }) as Arc<dyn PluginInvoker>,
            &["counter_plugin"],
        );
        let config = PipelineConfig {
            name: "loop_test".into(),
            loop_config: agentos_core::types::LoopConfig {
                enabled: true,
                max_iterations: -1, // 无限，靠 ended 退出
            },
            steps: vec![atomic_step("body", "counter_plugin")],
        };
        let state = executor
            .run(&config, &StepLibrary::default(), json!({}))
            .await
            .unwrap();
        // 验证循环到第 3 次因 ended 停止
        assert_eq!(counter.load(Ordering::SeqCst), 3);
        assert_eq!(state["round"], json!(3));
        assert_eq!(state["ended"], json!(true));
    }

    #[tokio::test]
    async fn test_loop_max_iterations_safety() {
        // max_iterations=2 安全阀生效；插件从不设 ended，应跑满 2 轮就停
        let counter = Arc::new(AtomicUsize::new(0));
        let executor = make_executor(
            Arc::new(CountingInvoker {
                counter: counter.clone(),
                stop_after: 0, // 永不 set ended
                set_suspended_after: 0,
            }) as Arc<dyn PluginInvoker>,
            &["p"],
        );
        let config = PipelineConfig {
            name: "loop_cap".into(),
            loop_config: agentos_core::types::LoopConfig {
                enabled: true,
                max_iterations: 2,
            },
            steps: vec![atomic_step("body", "p")],
        };
        let _ = executor
            .run(&config, &StepLibrary::default(), json!({}))
            .await
            .unwrap();
        assert_eq!(counter.load(Ordering::SeqCst), 2);
    }

    #[tokio::test]
    async fn test_routes() {
        // routes when 条件匹配后 set 字段 + ended
        let fixture = Fixture::build(&[]);
        let config = PipelineConfig {
            name: "routes".into(),
            loop_config: Default::default(),
            steps: vec![PipelineStep {
                id: "router".into(),
                steps: vec![], // 不调任何插件
                context: HashMap::new(),
                routes: vec![Route {
                    when: "core_type == 'tool_execute'".into(),
                    then: agentos_core::types::RouteAction {
                        next: RouteNext::End,
                        set: updates(&[("handled", json!(true))]),
                    },
                }],
                loop_config: None,
            }],
        };
        let library = StepLibrary::default();
        let state = fixture
            .run(&config, &library, json!({ "core_type": "tool_execute" }))
            .await;
        assert_eq!(state["handled"], json!(true));
        assert_eq!(state["ended"], json!(true));
    }

    #[tokio::test]
    async fn test_routes_wait() {
        // Wait → suspended=true
        let fixture = Fixture::build(&[]);
        let config = PipelineConfig {
            name: "wait".into(),
            loop_config: Default::default(),
            steps: vec![PipelineStep {
                id: "w".into(),
                steps: vec![],
                context: HashMap::new(),
                routes: vec![Route {
                    when: "True".into(),
                    then: agentos_core::types::RouteAction {
                        next: RouteNext::Wait,
                        set: HashMap::new(),
                    },
                }],
                loop_config: None,
            }],
        };
        let state = fixture
            .run(&config, &StepLibrary::default(), json!({}))
            .await;
        assert_eq!(state["suspended"], json!(true));
    }

    #[tokio::test]
    async fn test_miss_reported() {
        // step 引用不存在的 step id 和插件名，不 panic（error log）
        let fixture = Fixture::build(&[]);
        let config = PipelineConfig {
            name: "miss".into(),
            loop_config: Default::default(),
            steps: vec![
                atomic_step("s1", "ghost_step"), // 既不是 step 也不是已知插件
                atomic_step("s2", "ghost_plugin"),
            ],
        };
        // 不应 panic；返回的 state 仍合法
        let state = fixture
            .run(&config, &StepLibrary::default(), json!({}))
            .await;
        assert_eq!(state["ended"], json!(false));
    }

    #[tokio::test]
    async fn test_context_injection() {
        // step.context 的 {{state.xxx}} 被渲染注入到 state
        let fixture = Fixture::build(&["reader"]);
        // 插件读 ctx.state 里的 injected 字段，写回 state 确认注入生效
        fixture.invoker.set_result(
            "reader",
            PluginResult {
                state_updates: updates(&[("seen", json!("{{state.injected}}"))]),
                ..Default::default()
            },
        );
        // 注意：上面结果也会被渲染，但 state_updates merge 时不渲染。
        // 我们改为：context 注入后，直接用 state.injected 验证（不依赖插件）。
        // 这里再写一个 context 注入测试。
        let _ = fixture;
        let fixture2 = Fixture::build(&[]);
        let mut context = HashMap::new();
        context.insert(
            "injected".to_string(),
            json!("agent={{state.agent_id}}"),
        );
        let config = PipelineConfig {
            name: "ctx".into(),
            loop_config: Default::default(),
            steps: vec![PipelineStep {
                id: "ctx_step".into(),
                steps: vec![],
                context,
                routes: vec![Route {
                    when: "injected == 'agent=A1'".into(),
                    then: agentos_core::types::RouteAction {
                        next: RouteNext::End,
                        set: updates(&[("matched", json!(true))]),
                    },
                }],
                loop_config: None,
            }],
        };
        let state = fixture2
            .run(&config, &StepLibrary::default(), json!({ "agent_id": "A1" }))
            .await;
        // injected 模板被渲染为 "agent=A1"
        assert_eq!(state["injected"], json!("agent=A1"));
        // 路由条件命中（证明注入确实发生在路由求值之前）
        assert_eq!(state["matched"], json!(true));
        assert_eq!(state["ended"], json!(true));
    }

    #[tokio::test]
    async fn test_skip_remaining() {
        // 插件返回 skip_remaining=true → 跳过同一 step.steps 中后续项
        let fixture = Fixture::build(&["first", "second"]);
        fixture.invoker.set_result(
            "first",
            PluginResult {
                state_updates: updates(&[("first_done", json!(true))]),
                skip_remaining: true,
                ..Default::default()
            },
        );
        fixture.invoker.set_result(
            "second",
            PluginResult {
                state_updates: updates(&[("second_done", json!(true))]),
                ..Default::default()
            },
        );
        let config = PipelineConfig {
            name: "skip".into(),
            loop_config: Default::default(),
            steps: vec![PipelineStep {
                id: "both".into(),
                steps: vec!["first".into(), "second".into()],
                context: HashMap::new(),
                routes: vec![],
                loop_config: None,
            }],
        };
        let state = fixture
            .run(&config, &StepLibrary::default(), json!({}))
            .await;
        assert_eq!(state["first_done"], json!(true));
        // second 不应被调用
        assert_eq!(fixture.invoker.call_count("second"), 0);
        assert!(state.get("second_done").is_none());
    }

    #[tokio::test]
    async fn test_plugin_error_continues() {
        // 插件返回 error → warn + 继续后续 step（不 panic）
        let fixture = Fixture::build(&["bad", "good"]);
        fixture.invoker.set_result(
            "bad",
            PluginResult {
                error: Some(PluginError {
                    message: "boom".into(),
                    code: None,
                    source: None,
                }),
                ..Default::default()
            },
        );
        fixture.invoker.set_result(
            "good",
            PluginResult {
                state_updates: updates(&[("ok", json!(true))]),
                ..Default::default()
            },
        );
        let config = PipelineConfig {
            name: "err".into(),
            loop_config: Default::default(),
            steps: vec![PipelineStep {
                id: "seq".into(),
                steps: vec!["bad".into(), "good".into()],
                context: HashMap::new(),
                routes: vec![],
                loop_config: None,
            }],
        };
        let state = fixture
            .run(&config, &StepLibrary::default(), json!({}))
            .await;
        // good 仍被调用
        assert_eq!(fixture.invoker.call_count("good"), 1);
        assert_eq!(state["ok"], json!(true));
    }

    #[tokio::test]
    async fn test_suspended_stops_loop() {
        // 管道级 loop，插件 set suspended=true → 循环停止（不进入下一轮）
        let counter = Arc::new(AtomicUsize::new(0));
        let executor = make_executor(
            Arc::new(CountingInvoker {
                counter: counter.clone(),
                stop_after: 0,
                set_suspended_after: 2,
            }) as Arc<dyn PluginInvoker>,
            &["p"],
        );
        let config = PipelineConfig {
            name: "suspend_loop".into(),
            loop_config: agentos_core::types::LoopConfig {
                enabled: true,
                max_iterations: 10,
            },
            steps: vec![atomic_step("body", "p")],
        };
        let state = executor
            .run(&config, &StepLibrary::default(), json!({}))
            .await
            .unwrap();
        // 跑了 2 轮就停
        assert_eq!(counter.load(Ordering::SeqCst), 2);
        assert_eq!(state["suspended"], json!(true));
        // ended 未被设置
        assert_eq!(state["ended"], json!(false));
    }

    #[tokio::test]
    async fn test_dynamic_plugin_name() {
        // step.steps 里含 {{state.core_plugin}} 模板 → 渲染后命中插件
        let fixture = Fixture::build(&["real_core"]);
        fixture.invoker.set_result(
            "real_core",
            PluginResult {
                state_updates: updates(&[("executed", json!(true))]),
                ..Default::default()
            },
        );
        let config = PipelineConfig {
            name: "dyn".into(),
            loop_config: Default::default(),
            steps: vec![PipelineStep {
                id: "dyn_step".into(),
                steps: vec!["{{state.core_plugin}}".to_string()],
                context: HashMap::new(),
                routes: vec![],
                loop_config: None,
            }],
        };
        let state = fixture
            .run(
                &config,
                &StepLibrary::default(),
                json!({ "core_plugin": "real_core" }),
            )
            .await;
        assert_eq!(fixture.invoker.call_count("real_core"), 1);
        assert_eq!(state["executed"], json!(true));
    }

    #[tokio::test]
    async fn test_non_object_initial_state_becomes_object() {
        // 非对象 initial_state 应被规范化为空对象，不 panic
        let fixture = Fixture::build(&[]);
        let config = PipelineConfig {
            name: "noop".into(),
            loop_config: Default::default(),
            steps: vec![],
        };
        let state = fixture
            .run(&config, &StepLibrary::default(), serde_json::Value::Null)
            .await;
        assert!(state.is_object());
        assert_eq!(state["ended"], json!(false));
    }

    #[tokio::test]
    async fn test_route_step_next_recorded() {
        // RouteNext::Step(id) → 记到 state.next_step（简化语义）
        let fixture = Fixture::build(&[]);
        let config = PipelineConfig {
            name: "jump".into(),
            loop_config: Default::default(),
            steps: vec![PipelineStep {
                id: "j".into(),
                steps: vec![],
                context: HashMap::new(),
                routes: vec![Route {
                    when: "True".into(),
                    then: agentos_core::types::RouteAction {
                        next: RouteNext::Step("somewhere".into()),
                        set: HashMap::new(),
                    },
                }],
                loop_config: None,
            }],
        };
        let state = fixture
            .run(&config, &StepLibrary::default(), json!({}))
            .await;
        assert_eq!(state["next_step"], json!("somewhere"));
    }

    #[tokio::test]
    async fn test_step_level_loop() {
        // step 自带 loop_config（非管道级）：循环执行 step.steps
        let counter = Arc::new(AtomicUsize::new(0));
        let executor = make_executor(
            Arc::new(CountingInvoker {
                counter: counter.clone(),
                stop_after: 3,
                set_suspended_after: 0,
            }) as Arc<dyn PluginInvoker>,
            &["p"],
        );
        let config = PipelineConfig {
            name: "step_loop".into(),
            loop_config: Default::default(), // 管道级不循环
            steps: vec![PipelineStep {
                id: "looper".into(),
                steps: vec!["p".to_string()],
                context: HashMap::new(),
                routes: vec![],
                loop_config: Some(agentos_core::types::LoopConfig {
                    enabled: true,
                    max_iterations: -1,
                }),
            }],
        };
        let state = executor
            .run(&config, &StepLibrary::default(), json!({}))
            .await
            .unwrap();
        // step 自带循环跑了 3 轮（第 3 次 set ended）
        assert_eq!(counter.load(Ordering::SeqCst), 3);
        assert_eq!(state["ended"], json!(true));
    }
}
