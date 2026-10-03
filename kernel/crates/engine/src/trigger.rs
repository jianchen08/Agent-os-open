// @feature: FP-0.2.〇 管道引擎 | @vision: V3 可嵌入 | @ci: rust-test
//! 触发器注册表与 committed 视图求值（capability `trigger-svc` 的内核执行面）。
//!
//! 求值统一模型（ADR 2026-10-02-trigger-eval-unification）：条件触发器的求值、
//! 边沿、点火账本上收内核；条件表达式 = 内核 condition.rs（与管道 when/routes/
//! while 同一解析与求值，G9/G10 同源，不设第二套语法）。M1 只做 committed 视图
//! （store 写入口锚点，见 [`crate::store::SqliteStore`] 的 upsert 锚点注释）；
//! staged 视图（transient.set 挂钩）留 M2。
//!
//! 职责三分：
//! - **注册表**：条件 AST、watched keys、pipeline/owner 归属；
//! - **边沿簿记**：per-trigger last-boolean（注册时对当前 committed 行 state
//!   种子求值初始化）——边沿 false→true 才点火，持续满足不重复；
//! - **fire log**：点火账本（fire_seq 进程内递增 + ack 清账），reconcile
//!   兜底重投的数据源，ack 前保留。
//!
//! 已知限制（妥协契约，随模块复核）：fire log 当前**进程内存活，重启即丢**——
//! 重启后未 ack 的火不再重投，注册表本身同样不跨重启（触发器须由注册方重挂）。
//! 升级触发条件：出现"跨重启 at-least-once"硬需求（触发动作无法由 reconcile
//! 轮询方在重启后自行重建）时，fire log 与注册表落 SQLite（独立账本键/表，
//! 不混入 pipeline_state）。时间上限：M2（staged 视图接入）落地时复核一次，
//! 届时无此需求则在 ADR 记为长期接受态。
//!
//! [来源: docs/decisions/2026-10-02-trigger-eval-unification.md]
//! [来源: docs/decisions/2026-10-02-state-write-surface-convergence.md 决策4]

use std::collections::HashMap;
use std::sync::atomic::{AtomicI64, Ordering};
use std::sync::{Arc, OnceLock};

use parking_lot::RwLock;
use serde_json::Value;

use crate::condition::{eval_expr, Expr};

/// 触发器观察视图标识（M1 仅 committed；M2 接入 staged 后由注册声明区分）。
pub const VIEW_COMMITTED: &str = "committed";

/// 点火通知闭包：fire 产生时同步调用一次（fire log 已落账，通知失败不重试——
/// reconcile 周期轮询兜底）。锚点运行在存储驱动的专用线程池（无 tokio 上下文），
/// 实现方必须用构造期捕获的 runtime Handle 自行 spawn，不得依赖线程局部上下文。
pub type FireNotifier = Arc<dyn Fn(TriggerFire) + Send + Sync>;

/// 一次点火（fire log 条目 + 通知载荷）。
#[derive(Debug, Clone, PartialEq)]
pub struct TriggerFire {
    /// 进程内递增的点火序号（reconcile 的 fire_ids / ack 的对账键）。
    pub fire_seq: i64,
    pub trigger_id: String,
    /// 点火通知的接收方（注册时声明的 owner；通知通道见内核装配方）。
    pub owner_plugin_id: String,
    /// 触发本次求值的提交写所属管道。
    pub pipeline_id: String,
    /// 观察视图标识（[`VIEW_COMMITTED`]）。
    pub view: &'static str,
    /// 本次提交写中命中 watched-keys 的键（未声明 watched-keys = 全部 changed keys）。
    pub keys: Vec<String>,
    pub fired_at: String,
}

/// 注册请求（capability 层从参数解出；条件已由调用方 parse 为 AST）。
pub struct TriggerRegistration {
    pub trigger_id: String,
    /// 注册时租户（行 state 读取与写锚点租户过滤用）。
    pub tenant_id: String,
    /// 作用域管道；None = 全局触发器（任意管道的提交写都进入求值候选）。
    pub pipeline_id: Option<String>,
    /// 编译后条件 AST；None = 空表达式（condition.rs Ok(None) 语义，恒真）。
    pub condition: Option<Expr>,
    /// 观察键；空 = 不筛键（任意提交写都进入求值候选）。注册方应声明条件
    /// 引用的全部键——未声明的键变化不会触发求值（watched-keys ∩ changed-keys
    /// 是求值准入，不是求值语义的一部分）。
    pub watched_keys: Vec<String>,
    pub owner_plugin_id: String,
}

struct RegisteredTrigger {
    tenant_id: String,
    pipeline_id: Option<String>,
    condition: Option<Expr>,
    watched_keys: Vec<String>,
    owner_plugin_id: String,
    /// 边沿簿记：上次求值布尔（种子求值初始化）。
    last: bool,
}

#[derive(Default)]
struct Inner {
    triggers: HashMap<String, RegisteredTrigger>,
    /// 未 ack 点火账本（fire_seq 升序 = 老→新）。进程内存活（见文件头已知限制）。
    fire_log: Vec<TriggerFire>,
    notifier: Option<FireNotifier>,
}

/// 触发器注册表（注册 / 边沿簿记 / fire log / 通知分发）。
pub struct TriggerRegistry {
    inner: RwLock<Inner>,
    fire_seq: AtomicI64,
}

impl Default for TriggerRegistry {
    fn default() -> Self {
        Self::new()
    }
}

static GLOBAL_REGISTRY: OnceLock<TriggerRegistry> = OnceLock::new();

/// 获取全局 TriggerRegistry 单例（首次调用时惰性初始化）。
///
/// 全局单例理由与 transient 同款：锚点深在 store 驱动层，穿层注入会把
/// registry 句柄拖进 AppState/Executor 全部构造链（AppState 体积膨胀触发
/// Windows 主线程栈溢出，见 transient.rs 同款注释）。
pub fn global_registry() -> &'static TriggerRegistry {
    GLOBAL_REGISTRY.get_or_init(TriggerRegistry::new)
}

impl TriggerRegistry {
    pub fn new() -> Self {
        Self {
            inner: RwLock::new(Inner::default()),
            fire_seq: AtomicI64::new(0),
        }
    }

    /// 注册触发器并对 `seed_state`（当前 committed 行 state）做种子求值一次。
    ///
    /// 种子求值命中 = 边沿簿记初始化为真（返回 `seed_fired=true` 供调用方
    /// 自行处置），**不落 fire log 不点火**——注册是簿记初始化点，不是
    /// false→true 边沿；同 id 重注册 = 覆盖（边沿簿记随种子求值重建）。
    pub fn register(&self, reg: TriggerRegistration, seed_state: &Value) -> bool {
        let seed_fired = eval_condition(reg.condition.as_ref(), seed_state);
        let mut inner = self.inner.write();
        inner.triggers.insert(
            reg.trigger_id.clone(),
            RegisteredTrigger {
                tenant_id: reg.tenant_id,
                pipeline_id: reg.pipeline_id,
                condition: reg.condition,
                watched_keys: reg.watched_keys,
                owner_plugin_id: reg.owner_plugin_id,
                last: seed_fired,
            },
        );
        seed_fired
    }

    /// 注销触发器（边沿簿记随注册项一并清除；其未 ack 的 fire log 保留至 ack）。
    /// 返回是否确有该触发器。
    pub fn unregister(&self, trigger_id: &str) -> bool {
        self.inner.write().triggers.remove(trigger_id).is_some()
    }

    /// 在册触发器数（诊断/测试面）。
    pub fn registered_count(&self) -> usize {
        self.inner.read().triggers.len()
    }

    /// 注入点火通知闭包（内核装配方一次性接线；覆盖式设置）。
    pub fn set_notifier(&self, notifier: FireNotifier) {
        self.inner.write().notifier = Some(notifier);
    }

    /// committed 视图写锚点：存储驱动 upsert 提交成功后调用。
    ///
    /// `load_row` 提供写入后的**完整行 state**（committed 求值上下文），惰性
    /// 加载——仅当 watched-keys ∩ changed-keys 命中注册触发器时才调用：
    /// 无触发器/无命中 = 零读零求值，写路径零成本（生产常态）。
    /// 求值异常由 eval fail-soft 折假（condition.rs 契约），边沿 false→true
    /// 才落 fire log 并通知。
    pub fn on_committed_write(
        &self,
        tenant_id: &str,
        pipeline_id: &str,
        changed_keys: &[String],
        load_row: &dyn Fn() -> Option<Value>,
    ) {
        // 候选筛选（读锁）：pipeline 作用域（tenant 同域才算同一行）+
        // watched-keys ∩ changed-keys（未声明 watched-keys = 不筛键）。
        let candidates: Vec<String> = {
            let inner = self.inner.read();
            if inner.triggers.is_empty() {
                return; // 快路径：注册表空 = 写路径零成本
            }
            inner
                .triggers
                .iter()
                .filter(|(_, t)| {
                    t.pipeline_id
                        .as_deref()
                        .is_none_or(|pid| pid == pipeline_id && t.tenant_id == tenant_id)
                        && (t.watched_keys.is_empty()
                            || t.watched_keys.iter().any(|w| changed_keys.contains(w)))
                })
                .map(|(id, _)| id.clone())
                .collect()
        };
        if candidates.is_empty() {
            return;
        }
        // 命中才加载写入后的完整行 state（同一批候选共享一份求值上下文）。
        // 加载失败 = 本轮求值缺上下文：debug 留痕跳过（观察权不破坏提交权，
        // 锚点失败不得回滚已提交的写）。
        let Some(row) = load_row() else {
            tracing::debug!(
                pipeline_id,
                keys = ?changed_keys,
                "触发求值行 state 加载失败，跳过本轮 committed 求值"
            );
            return;
        };
        let notifier = self.inner.read().notifier.clone();
        let mut fired: Vec<TriggerFire> = Vec::new();
        {
            let mut inner = self.inner.write();
            for trigger_id in candidates {
                let Some(t) = inner.triggers.get_mut(&trigger_id) else {
                    continue; // 求值间隙被注销：跳过（下一写自然不再候选）
                };
                let now = eval_condition(t.condition.as_ref(), &row);
                let prev = t.last;
                t.last = now;
                if !prev && now {
                    let matched: Vec<String> = if t.watched_keys.is_empty() {
                        changed_keys.to_vec()
                    } else {
                        changed_keys
                            .iter()
                            .filter(|k| t.watched_keys.contains(k))
                            .cloned()
                            .collect()
                    };
                    fired.push(TriggerFire {
                        fire_seq: self.fire_seq.fetch_add(1, Ordering::SeqCst) + 1,
                        trigger_id,
                        owner_plugin_id: t.owner_plugin_id.clone(),
                        pipeline_id: pipeline_id.to_string(),
                        view: VIEW_COMMITTED,
                        keys: matched,
                        fired_at: chrono::Utc::now().to_rfc3339(),
                    });
                }
            }
            if !fired.is_empty() {
                inner.fire_log.extend(fired.iter().cloned());
            }
        }
        // 锁外通知：notifier 自主决定异步性；失败不重试（reconcile 兜底）。
        if let Some(notify) = notifier {
            for fire in fired {
                notify(fire);
            }
        }
    }

    /// 未 ack 的火（老→新）。幂等可重复——ack 前每次轮询都返回全量。
    pub fn reconcile(&self) -> Vec<TriggerFire> {
        self.inner.read().fire_log.clone()
    }

    /// ack：清账已投递确认的火。返回实际清除条数（未知 fire_seq 忽略）。
    pub fn ack(&self, fire_ids: &[i64]) -> usize {
        let mut inner = self.inner.write();
        let before = inner.fire_log.len();
        inner.fire_log.retain(|f| !fire_ids.contains(&f.fire_seq));
        before - inner.fire_log.len()
    }
}

/// 条件求值（边沿检测输入）。
///
/// None = 空表达式（注册期 parse_condition Ok(None)，无条件恒真——不是异常态）；
/// 求值异常兜底由 eval_expr 的 fail-soft 语义承载：算术操作数非数字/除零/
/// 未知函数等异常形态一律折 Null/False + warn 留痕（condition.rs 契约），
/// 对齐"条件求值异常 → 视为 False + 留痕"契约。
fn eval_condition(condition: Option<&Expr>, state: &Value) -> bool {
    match condition {
        None => true,
        Some(expr) => eval_expr(expr, state),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use std::sync::Mutex;

    /// 测试装载器桩：记录是否被调用（零成本契约的可观察面）。
    struct LoadProbe {
        row: Value,
        calls: Mutex<usize>,
    }

    impl LoadProbe {
        fn new(row: Value) -> Self {
            Self {
                row,
                calls: Mutex::new(0),
            }
        }

        fn load(&self) -> Option<Value> {
            *self.calls.lock().unwrap() += 1;
            Some(self.row.clone())
        }

        fn calls(&self) -> usize {
            *self.calls.lock().unwrap()
        }
    }

    fn reg(id: &str, condition: &str, watched: &[&str]) -> TriggerRegistration {
        TriggerRegistration {
            trigger_id: id.to_string(),
            tenant_id: "default".to_string(),
            pipeline_id: Some("pipe-t".to_string()),
            condition: Some(
                crate::condition::parse_condition(condition)
                    .unwrap()
                    .unwrap(),
            ),
            watched_keys: watched.iter().map(|s| s.to_string()).collect(),
            owner_plugin_id: "owner_ext".to_string(),
        }
    }

    /// 通知记录桩（同步收集，无时序等待）。
    type FireSink = Arc<Mutex<Vec<TriggerFire>>>;

    fn sink_notifier() -> (FireSink, FireNotifier) {
        let sink: FireSink = Arc::new(Mutex::new(Vec::new()));
        let s = sink.clone();
        (
            sink,
            Arc::new(move |f: TriggerFire| s.lock().unwrap().push(f)),
        )
    }

    // ── 注册与种子求值 ──────────────────────────────────────────

    #[test]
    fn test_register_seed_reflects_current_state() {
        let r = TriggerRegistry::new();
        // 种子命中：注册时条件已真 → seed_fired=true
        let fired = r.register(
            reg("t1", "tgx.status == 'done'", &["tgx.status"]),
            &json!({ "tgx.status": "done" }),
        );
        assert!(fired, "种子求值命中应返回 true");
        assert_eq!(r.registered_count(), 1);
        // 种子未命中
        let not_fired = r.register(
            reg("t2", "tgx.status == 'done'", &["tgx.status"]),
            &json!({ "tgx.status": "running" }),
        );
        assert!(!not_fired);
    }

    #[test]
    fn test_seed_hit_does_not_fire() {
        // 种子命中只是边沿簿记初始化（last=true），不落 fire log 不通知——
        // 注册是初始化点，非 false→true 边沿。
        let r = TriggerRegistry::new();
        let (sink, notifier) = sink_notifier();
        r.set_notifier(notifier);
        r.register(
            reg("t1", "tgx.status == 'done'", &["tgx.status"]),
            &json!({ "tgx.status": "done" }),
        );
        // 无关写不求值；命中写持续满足 → 边沿无 false→true 翻转
        r.on_committed_write("default", "pipe-t", &["tgx.other".to_string()], &|| None);
        r.on_committed_write("default", "pipe-t", &["tgx.status".to_string()], &|| {
            Some(json!({ "tgx.status": "done" }))
        });
        assert!(r.reconcile().is_empty(), "种子命中后持续满足不得点火");
        assert!(sink.lock().unwrap().is_empty());
    }

    #[test]
    fn test_register_replaces_same_id() {
        // 同 id 重注册 = 覆盖，条件与边沿簿记按新注册重建。
        let r = TriggerRegistry::new();
        r.register(reg("t1", "a == 1", &["a"]), &json!({ "a": 1 }));
        let fired = r.register(reg("t1", "a == 2", &["a"]), &json!({ "a": 2 }));
        assert!(fired, "重注册按新条件种子求值");
        assert_eq!(r.registered_count(), 1);
    }

    // ── 边沿语义 ────────────────────────────────────────────────

    #[test]
    fn test_edge_false_to_true_fires_once() {
        let r = TriggerRegistry::new();
        let (sink, notifier) = sink_notifier();
        r.set_notifier(notifier);
        r.register(
            reg("t1", "tgx.status == 'done'", &["tgx.status"]),
            &json!({ "tgx.status": "running" }),
        );
        // false→true：点火一次
        r.on_committed_write("default", "pipe-t", &["tgx.status".to_string()], &|| {
            Some(json!({ "tgx.status": "done" }))
        });
        // 持续满足：不重复
        r.on_committed_write("default", "pipe-t", &["tgx.status".to_string()], &|| {
            Some(json!({ "tgx.status": "done" }))
        });
        let fires = r.reconcile();
        assert_eq!(fires.len(), 1, "边沿只点一次火，持续满足不重复");
        assert_eq!(fires[0].trigger_id, "t1");
        assert_eq!(fires[0].keys, vec!["tgx.status"]);
        assert_eq!(fires[0].view, VIEW_COMMITTED);
        assert_eq!(fires[0].pipeline_id, "pipe-t");
        assert_eq!(sink.lock().unwrap().len(), 1, "通知与 fire log 同步落账");
    }

    #[test]
    fn test_edge_rearms_after_false() {
        // 性质：边沿可重复——true→false 复位后再次 false→true 再点火。
        let r = TriggerRegistry::new();
        r.register(
            reg("t1", "flag == True", &["flag"]),
            &json!({ "flag": false }),
        );
        for row in [
            json!({ "flag": true }),  // 1: false→true 点火
            json!({ "flag": true }),  // 2: 持续不点
            json!({ "flag": false }), // 3: 复位
            json!({ "flag": false }), // 4: 持续假不点
            json!({ "flag": true }),  // 5: 再次点火
        ] {
            r.on_committed_write("default", "pipe-t", &["flag".to_string()], &|| {
                Some(row.clone())
            });
        }
        let fires = r.reconcile();
        assert_eq!(fires.len(), 2, "两次边沿各点一次");
        assert!(
            fires[0].fire_seq < fires[1].fire_seq,
            "fire log 老→新（seq 递增）"
        );
    }

    // ── watched-keys 与 pipeline 过滤 ───────────────────────────

    #[test]
    fn test_watched_keys_filter_skips_unrelated_writes() {
        // 无关写不触发且不求值（零读：load_row 不被调用）。
        let r = TriggerRegistry::new();
        r.register(reg("t1", "a == 1", &["a"]), &json!({}));
        let probe = LoadProbe::new(json!({ "b": 1, "a": 1 }));
        r.on_committed_write("default", "pipe-t", &["b".to_string()], &|| {
            Some(probe.load().unwrap())
        });
        assert!(r.reconcile().is_empty());
        assert_eq!(probe.calls(), 0, "watched-keys 未命中不得加载行 state");
        // 命中后才加载并求值
        r.on_committed_write("default", "pipe-t", &["a".to_string()], &|| {
            Some(probe.load().unwrap())
        });
        assert_eq!(r.reconcile().len(), 1);
        assert!(probe.calls() > 0);
    }

    #[test]
    fn test_empty_watched_keys_observes_all_writes() {
        // 未声明 watched-keys = 不筛键（全部提交写进入求值候选）。
        let r = TriggerRegistry::new();
        r.register(
            TriggerRegistration {
                watched_keys: vec![],
                ..reg("t1", "a == 1", &[])
            },
            &json!({}),
        );
        r.on_committed_write("default", "pipe-t", &["x".to_string()], &|| {
            Some(json!({ "a": 1 }))
        });
        assert_eq!(r.reconcile().len(), 1);
        // keys = 全部 changed keys（无 watched 白名单可交）
        assert_eq!(r.reconcile()[0].keys, vec!["x"]);
    }

    #[test]
    fn test_pipeline_scope_filters_other_pipelines() {
        let r = TriggerRegistry::new();
        r.register(reg("t1", "a == 1", &["a"]), &json!({}));
        // 其他管道的写不进候选
        r.on_committed_write("default", "pipe-other", &["a".to_string()], &|| {
            Some(json!({ "a": 1 }))
        });
        assert!(r.reconcile().is_empty());
        // 作用域管道命中
        r.on_committed_write("default", "pipe-t", &["a".to_string()], &|| {
            Some(json!({ "a": 1 }))
        });
        assert_eq!(r.reconcile().len(), 1);
    }

    #[test]
    fn test_global_trigger_observes_any_pipeline() {
        // pipeline_id 缺省 = 全局触发器：任意管道的提交写都进入求值候选。
        let r = TriggerRegistry::new();
        r.register(
            TriggerRegistration {
                pipeline_id: None,
                ..reg("t1", "a == 1", &["a"])
            },
            &json!({}),
        );
        r.on_committed_write("default", "pipe-x", &["a".to_string()], &|| {
            Some(json!({ "a": 1 }))
        });
        assert_eq!(r.reconcile().len(), 1);
        assert_eq!(r.reconcile()[0].pipeline_id, "pipe-x");
    }

    // ── 求值上下文 = 写入后的完整行 state ───────────────────────

    #[test]
    fn test_evaluates_full_row_not_only_changed_keys() {
        // 条件引用两个键，watched 只声明其一：求值必须拿到写入后的完整行
        //（先写的键已在行内），否则合取条件恒假。
        let r = TriggerRegistry::new();
        r.register(
            reg("t1", "tgx.a == 1 and tgx.b == 2", &["tgx.b"]),
            &json!({ "tgx.a": 1 }),
        );
        // 只写 b=2（行内已有 a=1）→ 完整行求值命中
        r.on_committed_write("default", "pipe-t", &["tgx.b".to_string()], &|| {
            Some(json!({ "tgx.a": 1, "tgx.b": 2 }))
        });
        assert_eq!(r.reconcile().len(), 1);
        assert_eq!(
            r.reconcile()[0].keys,
            vec!["tgx.b"],
            "keys = 命中的 changed keys"
        );
    }

    // ── 求值异常 → False（fail-soft）───────────────────────────

    #[test]
    fn test_eval_anomaly_is_false_never_fires() {
        // 语法合法但求值 fail-soft 的条件（未知函数/非数字算术）恒判假，
        // 不 panic 不点火——"求值异常 → False" 契约。
        let r = TriggerRegistry::new();
        for cond in ["no_such_fn() > 0", "missing + 1 > 0", "a < 's'"] {
            let id = format!("t-{cond}");
            r.register(reg(&id, cond, &["a"]), &json!({ "a": 1 }));
            r.on_committed_write("default", "pipe-t", &["a".to_string()], &|| {
                Some(json!({ "a": 1 }))
            });
            assert!(
                r.reconcile().iter().all(|f| f.trigger_id != id),
                "{cond} 求值异常应判假不点火"
            );
            r.unregister(&id);
        }
    }

    // ── fire log / ack / reconcile ─────────────────────────────

    #[test]
    fn test_reconcile_until_acked_then_disappears() {
        let r = TriggerRegistry::new();
        r.register(reg("t1", "a == 1", &["a"]), &json!({}));
        r.on_committed_write("default", "pipe-t", &["a".to_string()], &|| {
            Some(json!({ "a": 1 }))
        });
        r.on_committed_write("default", "pipe-t", &["a".to_string()], &|| {
            Some(json!({ "a": 0 }))
        });
        r.on_committed_write("default", "pipe-t", &["a".to_string()], &|| {
            Some(json!({ "a": 1 }))
        });
        // ack 前每次 reconcile 都可见（幂等重投）
        let first = r.reconcile();
        assert_eq!(first.len(), 2);
        assert_eq!(r.reconcile().len(), 2, "ack 前 reconcile 幂等可见");
        // 只 ack 第一条：第二条保留
        let acked = r.ack(&[first[0].fire_seq]);
        assert_eq!(acked, 1);
        let rest = r.reconcile();
        assert_eq!(rest, vec![first[1].clone()], "未 ack 的火保留（老→新）");
        // 未知 fire_seq 忽略
        assert_eq!(r.ack(&[999_999]), 0);
        // 全部 ack 后消失
        r.ack(&[rest[0].fire_seq]);
        assert!(r.reconcile().is_empty(), "ack 后 reconcile 不再返回");
    }

    #[test]
    fn test_unregister_keeps_unacked_fires() {
        // 注销触发器不清 fire log：未 ack 的火仍可 reconcile/ack（at-least-once）。
        let r = TriggerRegistry::new();
        r.register(reg("t1", "a == 1", &["a"]), &json!({}));
        r.on_committed_write("default", "pipe-t", &["a".to_string()], &|| {
            Some(json!({ "a": 1 }))
        });
        assert!(r.unregister("t1"));
        assert_eq!(r.registered_count(), 0);
        assert_eq!(r.reconcile().len(), 1);
        assert_eq!(r.ack(&[r.reconcile()[0].fire_seq]), 1);
        assert!(r.reconcile().is_empty());
    }

    #[test]
    fn test_unregister_missing_is_false() {
        let r = TriggerRegistry::new();
        assert!(!r.unregister("never-registered"));
    }

    // ── 零成本快路径 ────────────────────────────────────────────

    #[test]
    fn test_zero_cost_when_no_triggers_registered() {
        // 无触发器注册：写锚点直接返回，不加载行 state（生产常态零成本）。
        let r = TriggerRegistry::new();
        let probe = LoadProbe::new(json!({}));
        r.on_committed_write("default", "pipe-t", &["a".to_string()], &|| {
            Some(probe.load().unwrap())
        });
        assert_eq!(probe.calls(), 0, "注册表空不得触发行加载");
        assert!(r.reconcile().is_empty());
    }
}
