// @feature: FP-0.2.内核内存治理 统一叠加压力风暴（多源并发+种子随机+M1 有界断言） | @ci: rust-test
//! 统一内存压力风暴：内核真实主要场景**叠加并发**跑（非逐个串行），全程 M1
//! 采样，断言 commit 有界、非单调增长、静置回落。
//!
//! ## 场景来源（2026-10-29 装机库 trace 面普查，真实负载画像）
//!
//! 主管道执行循环占绝对大头（prepare 514/llm 507/tool_input 463/tool_exec 461/
//! post 962，全部 state_update 落盘），任务生命周期次之，插件机器面在日志层；
//! 前后端走 WS 连接面（/ws/chat）。据此六源叠加：
//!
//! 1. **store_actor**——真实 `SqliteStore`（temp 文件 DB，WAL 真实落盘）：
//!    run_start 生命周期 + ~20KB prompt 形态 state 批量写 + 回读 + blob 工件；
//! 2. **plugin_fs_actor + watcher**——真实 `PluginWatcher::spawn` 生产循环
//!    （notify+poll、F17 迟滞、复验重注册全链路）：随机触碰插件代码文件，
//!    幻影单翻与真实双翻混合；
//! 3. **big_buffer_actor**——LLM 流式组装形态的 1~12MB 瞬时大块
//!    （build→hold→drop，昨晚实测 churn 形态，T1 保留窗的回归面）；
//! 4. **ledger_actor**——G2 契约账本 gate 状态往复（复验记账面）；
//! 5. **manifests_actor**——共享 manifests store 写读（热发现合并面）；
//! 6. **ws_connection_actor**——真实 axum 服务（in-memory 库+session 装配）+
//!    真实 WS 连接 churn：签票→握手→心跳/入站帧/大帧（64~256KB）→多数暴力
//!    断连（无 Close 帧）→重连，8~16 连接滚动池（前后端连接面 churn）。
//!
//! ## 随机叠加
//!
//! 各 actor 独立 xorshift 种子（MEM_STORM_SEED 派生，默认定值=可复现），步进
//! 间隔随机化；六源并发叠加 + 采样器同 tick 拍照——**不是逐场景串行**。
//!
//! ## 跑法
//!
//! - 有界档（常规 rust-test 车道，默认 12s）：本文件 `memory_super_storm_bounded`；
//! - 全量档（手动）：`cargo test -p agentos-integration-tests --test
//!   memory_super_storm -- --ignored --nocapture`（默认 90s，MEM_STORM_SECS 覆盖）。

use std::{
    collections::HashSet,
    net::SocketAddr,
    path::PathBuf,
    sync::Arc,
    time::{Duration, Instant},
};

use agentos_api::allocator::{collect_now, install_global_allocator, snapshot_stats};
use agentos_api::plugin_watcher::{ManifestsStore, PluginWatcher};
use agentos_core::traits::StorageBackend;
use agentos_core::traits::{HookContext, LifecycleHook, PluginInvoker, PluginManifest};
use agentos_core::types::PendingInputSource;
use agentos_core::types::{PluginContext, PluginError, PluginResult, ToolExecutionResult};
use agentos_engine::store::SqliteStore;
use agentos_plugin_loader::registry::CapabilityRegistryImpl;
use agentos_session::router::{InboundRouter, PipelineDispatcher};
use agentos_session::SessionCoordinator;
use axum::body::{to_bytes, Body};
use axum::http::{Request, StatusCode};
use futures_util::SinkExt;
use futures_util::StreamExt;
use serde_json::json;
use tokio_tungstenite::tungstenite::Message;
use tower::ServiceExt;

// ── 种子随机（xorshift64，零依赖确定性） ─────────────────────────────────────

struct XorShift(u64);
impl XorShift {
    fn new(seed: u64) -> Self {
        Self(seed.max(1))
    }
    fn next_u64(&mut self) -> u64 {
        let mut x = self.0;
        x ^= x << 13;
        x ^= x >> 7;
        x ^= x << 17;
        self.0 = x;
        x
    }
    /// [lo, hi) 均匀
    fn range(&mut self, lo: u64, hi: u64) -> u64 {
        lo + self.next_u64() % (hi - lo).max(1)
    }
}

// ── 采样 ────────────────────────────────────────────────────────────────────

#[derive(Debug, Clone, Copy)]
struct Sample {
    commit_mb: u64,
    purged_mb: u64,
}

fn sample() -> Sample {
    let s = snapshot_stats();
    Sample {
        commit_mb: s.process_commit_bytes.unwrap_or(0) / (1024 * 1024),
        purged_mb: s.purged_bytes.unwrap_or(0) / (1024 * 1024),
    }
}

// ── actor 1：真实 store（主管道循环落盘面 + blob 工件面） ───────────────────

async fn store_actor(
    store: Arc<SqliteStore>,
    mut rng: XorShift,
    stop: Arc<std::sync::atomic::AtomicBool>,
    warmup_iters: Option<u64>,
) -> Result<(), String> {
    let tenant = "storm_tenant";
    let mut i: u64 = 0;
    while !stop.load(std::sync::atomic::Ordering::Relaxed) {
        i += 1;
        if warmup_iters == Some(i - 1) {
            return Ok(());
        }
        let pid = format!("storm_pipe_{}", i % 8); // 8 条管道轮转（十几条量级）
        let run_id = format!("run_{i}");
        // ① 生命周期：run_start（running 态落盘）
        store
            .record_run_start(
                &pid,
                tenant,
                &run_id,
                "cfg_hash_storm",
                &chrono::Utc::now().to_rfc3339(),
            )
            .map_err(|e| e.to_string())?;
        // ② prompt 形态 state 批量写（~20KB 载荷，生产 dynamic_vars 量级）
        let mut fields = serde_json::Map::new();
        let filler = "x".repeat(rng.range(8 * 1024, 24 * 1024) as usize);
        fields.insert("prompt.dynamic_vars".into(), json!({"content": filler}));
        fields.insert("track.total_tokens".into(), json!(rng.range(1000, 100_000)));
        fields.insert("run_status".into(), json!("running"));
        store
            .upsert_state_fields(&pid, tenant, &fields)
            .map_err(|e| e.to_string())?;
        // ③ 回读（读路径同频）
        let _ = store.load_pipeline_state(&pid, tenant);
        // ④ blob 工件（64~256KB，产物面）
        let blob: Vec<u8> = (0..rng.range(64 * 1024, 256 * 1024))
            .map(|j| (j % 251) as u8)
            .collect();
        let _ = store.store_blob(&blob, "application/octet-stream");
        // ⑤ 终态：run_finalize 形态（状态覆盖）
        store
            .upsert_state_field(&pid, tenant, "run_status", &json!("completed"))
            .map_err(|e| e.to_string())?;
        // 随机步进（1~12ms，叠加节奏失拍）
        tokio::time::sleep(Duration::from_millis(rng.range(1, 12))).await;
    }
    Ok(())
}

// ── actor 2：插件文件随机触碰（watcher 生产循环由 spawn 独立驱动） ───────────

async fn plugin_fs_actor(
    stage_file: PathBuf,
    mut rng: XorShift,
    stop: Arc<std::sync::atomic::AtomicBool>,
) -> Result<(), String> {
    let mut gen: u64 = 0;
    // 混合节奏：多数 100~400ms（幻影级单翻），偶发 3~8ms 连击（真实编辑形态）
    while !stop.load(std::sync::atomic::Ordering::Relaxed) {
        gen += 1;
        let burst = rng.next_u64() % 8 == 0;
        let n = if burst { rng.range(2, 4) } else { 1 };
        for k in 0..n {
            let body = format!("impl gen={gen} k={k} {}", "y".repeat(64));
            let _ = std::fs::write(&stage_file, body.as_bytes());
            if k + 1 < n {
                tokio::time::sleep(Duration::from_millis(3)).await;
            }
        }
        tokio::time::sleep(Duration::from_millis(rng.range(100, 400))).await;
    }
    Ok(())
}

// ── actor 3：LLM 流式组装形态大块（1~12MB build→hold→drop） ─────────────────

async fn big_buffer_actor(
    mut rng: XorShift,
    stop: Arc<std::sync::atomic::AtomicBool>,
) -> Result<(), String> {
    while !stop.load(std::sync::atomic::Ordering::Relaxed) {
        let size = rng.range(1 << 20, 12 << 20) as usize;
        let mut buf = String::with_capacity(size);
        let chunk = "stream-chunk-".repeat(64);
        while buf.len() < size {
            buf.push_str(&chunk);
        }
        // 短暂持有（流式回放窗）
        tokio::time::sleep(Duration::from_millis(rng.range(10, 50))).await;
        drop(buf);
        tokio::time::sleep(Duration::from_millis(rng.range(5, 30))).await;
    }
    Ok(())
}

// ── actor 4+5：契约账本 + manifests store（复验记账与热发现合并面） ─────────

async fn ledger_actor(
    ledger: Arc<agentos_api::contract::ContractLedger>,
    m: agentos_core::traits::PluginManifest,
    mut rng: XorShift,
    stop: Arc<std::sync::atomic::AtomicBool>,
) -> Result<(), String> {
    let mut i: u64 = 0;
    while !stop.load(std::sync::atomic::Ordering::Relaxed) {
        i += 1;
        let st = if i % 2 == 0 {
            agentos_api::contract::PluginContractState::verify_failed(
                &m,
                true,
                format!("storm wave {i}"),
            )
        } else {
            agentos_api::contract::PluginContractState::verify_failed(
                &m,
                false,
                format!("storm wave {i}"),
            )
        };
        ledger.upsert(st);
        tokio::time::sleep(Duration::from_millis(rng.range(2, 20))).await;
    }
    Ok(())
}

async fn manifests_actor(
    store: ManifestsStore,
    m: agentos_core::traits::PluginManifest,
    mut rng: XorShift,
    stop: Arc<std::sync::atomic::AtomicBool>,
) -> Result<(), String> {
    while !stop.load(std::sync::atomic::Ordering::Relaxed) {
        {
            let mut guard = store.write().await;
            guard.retain(|x| x.id != m.id);
            guard.push(m.clone());
        }
        let _ = store.read().await.len();
        tokio::time::sleep(Duration::from_millis(rng.range(3, 25))).await;
    }
    Ok(())
}

// ── actor 6：前后端 WS 连接面 churn（真实服务 + 真实连接） ───────────────────

const WS_ADMIN_PW: &str = "storm-admin-pw-2026";

/// 入站分发桩：dispatch 即返（管道语义不在本车道目标内，连接面才是）。
struct StormDispatcher;

#[async_trait::async_trait]
impl PipelineDispatcher for StormDispatcher {
    async fn dispatch_user_input(
        &self,
        _thread_id: &str,
        _user_id: &str,
        _content: &str,
        _pipeline_id: &str,
        _thinking_strength: &str,
        _execution_context: Option<&serde_json::Value>,
        _state_overlay: Option<&serde_json::Value>,
        _agent_id: &str,
        _pipeline_config_id: Option<&str>,
        _cmid: &str,
        _source: PendingInputSource,
    ) -> Result<(), String> {
        Ok(())
    }
    async fn dispatch_interaction_response(
        &self,
        _thread_id: &str,
        _request_id: &str,
        _response: &serde_json::Value,
    ) -> Result<(), String> {
        Ok(())
    }
    async fn dispatch_stop(&self, _thread_id: &str, _pipeline_id: &str) -> Result<(), String> {
        Ok(())
    }
}

type WsStream =
    tokio_tungstenite::WebSocketStream<tokio_tungstenite::MaybeTlsStream<tokio::net::TcpStream>>;

/// 真实 WS 服务面：in-memory 库 + 播种 admin + session 装配 + axum 真实 TCP。
struct WsFixture {
    addr: SocketAddr,
    app: axum::Router,
    token: String,
}

async fn spawn_ws_fixture() -> WsFixture {
    let store = Arc::new(SqliteStore::open_memory().expect("memory store"));
    store
        .create_user(&agentos_core::types::UserRecord {
            user_id: "00000000-0000-0000-0000-0000000000a6".to_string(),
            username: "storm_admin".to_string(),
            password: agentos_http::auth::hash_password(WS_ADMIN_PW).unwrap(),
            email: Some("storm@agentos.dev".to_string()),
            role: "admin".to_string(),
            tenant_id: "default".to_string(),
            created_at: chrono::Utc::now().to_rfc3339(),
            last_login_at: None,
            must_change_password: false,
        })
        .await
        .expect("seed admin");
    let mut state = agentos_api::routes::AppState::new();
    state.store = Some(store);
    state.session = Some(Arc::new(SessionCoordinator::new()));
    state.inbound_router = Some(Arc::new(InboundRouter::new(Arc::new(StormDispatcher))));
    let app = agentos_api::server::build_router(state.clone());
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
        .await
        .expect("bind ephemeral");
    let addr = listener.local_addr().unwrap();
    tokio::spawn(async move {
        axum::serve(listener, agentos_api::server::build_router(state))
            .await
            .expect("ws fixture serve");
    });
    // 登录取 token（真实认证路径）
    let resp = app
        .clone()
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/api/v1/auth/login")
                .header("content-type", "application/json")
                .body(Body::from(
                    json!({"username": "storm_admin", "password": WS_ADMIN_PW}).to_string(),
                ))
                .unwrap(),
        )
        .await
        .expect("login oneshot");
    assert_eq!(resp.status(), StatusCode::OK, "播种 admin 必须可登录");
    let bytes = to_bytes(resp.into_body(), 8192).await.unwrap();
    let v: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
    WsFixture {
        addr,
        app,
        token: v["access_token"]
            .as_str()
            .expect("access_token")
            .to_string(),
    }
}

impl WsFixture {
    /// 签一次性票（真实签发路径）。
    async fn issue_ticket(&self) -> Result<String, String> {
        let resp = self
            .app
            .clone()
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/api/v1/ws-ticket")
                    .header("authorization", format!("Bearer {}", self.token))
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .map_err(|e| e.to_string())?;
        if resp.status() != StatusCode::OK {
            return Err(format!("签票失败: {}", resp.status()));
        }
        let bytes = to_bytes(resp.into_body(), 8192)
            .await
            .map_err(|e| e.to_string())?;
        let v: serde_json::Value = serde_json::from_slice(&bytes).map_err(|e| e.to_string())?;
        Ok(v["ticket"]
            .as_str()
            .ok_or_else(|| "缺 ticket 字段".to_string())?
            .to_string())
    }

    /// 签票→握手一条真实连接。
    async fn connect(&self) -> Result<WsStream, String> {
        let ticket = self.issue_ticket().await?;
        let url = format!("ws://{}/ws/chat?ticket={}", self.addr, ticket);
        let (ws, _) = tokio_tungstenite::connect_async(url)
            .await
            .map_err(|e| e.to_string())?;
        Ok(ws)
    }
}

/// 连接 churn：8~16 连接滚动池；每步随机「补连/发帧（心跳·入站·畸形·64~256KB
/// 大帧）/机会性读排空/暴力断连（无 Close，模拟前端断电）」。thread_id 取 16 值
/// 轮转——session 侧 thread 映射按设计有界，本车道钉的是连接面缓冲 churn，
/// 唯一 id 无界增长属语义堆面，另案。
async fn ws_connection_actor(
    fx: Arc<WsFixture>,
    mut rng: XorShift,
    stop: Arc<std::sync::atomic::AtomicBool>,
) -> Result<(), String> {
    let mut pool: Vec<WsStream> = Vec::new();
    let mut frame_no: u64 = 0;
    while !stop.load(std::sync::atomic::Ordering::Relaxed) {
        frame_no += 1;
        // 补连（签票+真实握手）
        let target = 8 + (rng.next_u64() % 9) as usize; // 8..=16
        if pool.len() < target {
            match fx.connect().await {
                Ok(ws) => pool.push(ws),
                Err(e) => return Err(format!("连接失败: {e}")),
            }
        }
        // 随机发帧
        let idx = (rng.next_u64() as usize) % pool.len();
        let text = match rng.next_u64() % 4 {
            0 => json!({"type": "heartbeat"}).to_string(),
            1 => {
                let tid = format!("storm-thread-{}", frame_no % 16);
                json!({
                    "type": "user_input",
                    "thread_id": tid,
                    "content": format!("storm 负载帧 {frame_no} {}", "c".repeat(rng.range(64, 512) as usize)),
                    "client_message_id": format!("cmid-{frame_no}"),
                })
                .to_string()
            }
            2 => "not-json{{{".to_string(), // 畸形帧丢弃路径
            _ => json!({
                "type": "heartbeat",
                "pad": "p".repeat(rng.range(64 * 1024, 256 * 1024) as usize),
            })
            .to_string(), // 大帧（帧缓冲分配面）
        };
        if pool[idx].send(Message::Text(text.into())).await.is_err() {
            pool.swap_remove(idx); // 对端已断，滚动池收缩
            continue;
        }
        // 机会性读排空（ack/错误帧回程），30ms 上限防阻塞
        let ridx = (rng.next_u64() as usize) % pool.len();
        match tokio::time::timeout(Duration::from_millis(30), pool[ridx].next()).await {
            Ok(Some(Ok(_))) => {}
            Ok(Some(Err(_))) | Ok(None) => {
                pool.swap_remove(ridx);
            }
            Err(_) => {} // 无帧可读
        }
        // 偶发暴力断连（无 Close 帧——服务端清理路径的真负载形态）
        if pool.len() > 10 && rng.next_u64() % 6 == 0 {
            let k = (rng.next_u64() as usize) % pool.len();
            pool.swap_remove(k);
        }
        tokio::time::sleep(Duration::from_millis(rng.range(2, 15))).await;
    }
    Ok(()) // pool drop = 存量连接全部暴力断
}

// ── 最小 invoker 桩（watcher 生产循环所需全 trait） ─────────────────────────

/// 每轮 discover 恒上报同一 light 管道清单（boot 已注册形态），sync 走 GAP-6
/// 复验路径；零工具成员全程零 G2 探测/零调用（B15-R1 契约）。
struct StormInvoker {
    manifest: PluginManifest,
}

#[async_trait::async_trait]
impl PluginInvoker for StormInvoker {
    async fn invoke_pipeline_plugin<'a>(
        &self,
        _p: &str,
        _c: &PluginContext<'a>,
    ) -> Result<PluginResult, PluginError> {
        unreachable!("storm 场景零管道调用")
    }
    async fn invoke_tool(
        &self,
        _p: &str,
        _t: &str,
        _i: &serde_json::Value,
    ) -> Result<ToolExecutionResult, PluginError> {
        unreachable!("storm 场景零工具调用")
    }
    async fn send_lifecycle_hook(
        &self,
        _p: &str,
        _h: LifecycleHook,
        _c: &HookContext,
    ) -> Result<(), PluginError> {
        Ok(())
    }
    async fn discover_new_plugins(&self) -> Result<Vec<PluginManifest>, PluginError> {
        Ok(vec![self.manifest.clone()])
    }
    async fn list_plugin_tools(&self, _p: &str) -> Result<serde_json::Value, PluginError> {
        Ok(json!({ "tools": [] }))
    }
    async fn force_unload(&self, _plugin_id: &str) -> Result<(), PluginError> {
        Ok(())
    }
}

// ── 编排：六源叠加 + 采样 + 有界断言 ────────────────────────────────────────

fn run_storm(secs: u64, max_growth_mb: u64, quiet_slack_mb: u64) {
    install_global_allocator();
    let seed: u64 = std::env::var("MEM_STORM_SEED")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(0x00C0_FFEE_2026);
    let rt = tokio::runtime::Builder::new_multi_thread()
        .worker_threads(4)
        .enable_all()
        .build()
        .expect("runtime");
    rt.block_on(async move {
        let db_dir = tempfile::tempdir().expect("db tempdir");
        let db_path = db_dir.path().join("storm.db");
        let store = Arc::new(SqliteStore::open(db_path.to_str().expect("utf8")).expect("store"));

        // watcher 真实生产循环：light 管道插件（零工具，无 G2 探测），代码文件
        // 由 plugin_fs_actor 随机触碰——F17 迟滞/复验/重注册全链路在负载下运转。
        let plugins_dir = tempfile::tempdir().expect("plugins tempdir");
        let stage_dir = tempfile::tempdir().expect("stage tempdir");
        let pid = "storm_light";
        let manifest_json = json!({
            "id": pid, "name": pid, "version": "1.0.0",
            "plugin_type": "pipeline", "language": "python",
            "host_type": "sidecar", "entry": "python plugin.py",
            "host_group": "light", "invoke_entry": "execute",
            "capabilities": {},
        });
        let plugin_sub = plugins_dir.path().join(pid);
        std::fs::create_dir_all(&plugin_sub).unwrap();
        std::fs::write(
            plugin_sub.join("plugin.json"),
            serde_json::to_string_pretty(&manifest_json).unwrap(),
        )
        .unwrap();
        let stage_file = stage_dir.path().join("impl.py");
        std::fs::write(&stage_file, b"impl gen=0").unwrap();
        let m: agentos_core::traits::PluginManifest =
            serde_json::from_value(manifest_json).expect("manifest");
        let stage_for_resolver = stage_dir.path().to_path_buf();
        let resolver: Arc<agentos_api::plugin_watcher::CodeDirResolver> =
            Arc::new(move |_id: &str| Some(stage_for_resolver.clone()));

        let watcher = PluginWatcher::new(
            plugins_dir.path().to_path_buf(),
            Arc::new(StormInvoker {
                manifest: m.clone(),
            }),
            Arc::new(CapabilityRegistryImpl::new()),
            HashSet::from([pid.to_string()]),
        )
        .with_code_dir_resolver(resolver)
        .with_debounce(Duration::from_millis(10))
        .with_poll_interval(Duration::from_millis(50))
        .spawn();

        let ledger = Arc::new(agentos_api::contract::ContractLedger::new());
        let manifests: agentos_api::plugin_watcher::ManifestsStore =
            Arc::new(tokio::sync::RwLock::new(vec![m.clone()]));
        let stop = Arc::new(std::sync::atomic::AtomicBool::new(false));

        // WS 服务面（真实 axum + in-memory 库 + session 装配），基线前起——
        // 装配分配属一次性爬坡，不计入增长界。
        let ws_fx = Arc::new(spawn_ws_fixture().await);

        // 预热一轮 + collect：基线（排除首啟分配的一次性爬坡）
        store_actor(store.clone(), XorShift::new(seed), stop.clone(), Some(3))
            .await
            .unwrap();
        {
            // WS 预热：一条连接走完整签票→握手→心跳→断
            let mut warm = ws_fx.connect().await.expect("ws warmup connect");
            let _ = warm
                .send(Message::Text(
                    json!({"type": "heartbeat"}).to_string().into(),
                ))
                .await;
            let _ = tokio::time::timeout(Duration::from_millis(200), warm.next()).await;
        }
        collect_now();
        tokio::time::sleep(Duration::from_millis(200)).await;
        let baseline = sample();
        println!("storm baseline: {baseline:?}");

        // 六源叠加并发（随机节奏各自独立）
        let stop2 = stop.clone();
        let mut set = tokio::task::JoinSet::new();
        set.spawn(store_actor(
            store.clone(),
            XorShift::new(seed ^ 0xA1),
            stop2.clone(),
            None,
        ));
        set.spawn(big_buffer_actor(XorShift::new(seed ^ 0xB2), stop2.clone()));
        set.spawn(ledger_actor(
            ledger.clone(),
            m.clone(),
            XorShift::new(seed ^ 0xC3),
            stop2.clone(),
        ));
        set.spawn(manifests_actor(
            manifests.clone(),
            m.clone(),
            XorShift::new(seed ^ 0xD4),
            stop2.clone(),
        ));
        set.spawn(plugin_fs_actor(
            stage_file,
            XorShift::new(seed ^ 0xE5),
            stop2.clone(),
        ));
        set.spawn(ws_connection_actor(
            ws_fx,
            XorShift::new(seed ^ 0xF6),
            stop2,
        ));

        // 采样主循环（500ms 一拍）
        let deadline = Instant::now() + Duration::from_secs(secs);
        let mut series: Vec<Sample> = Vec::new();
        while Instant::now() < deadline {
            tokio::time::sleep(Duration::from_millis(500)).await;
            let s = sample();
            println!("storm t+{}s: {:?}", series.len() / 2, s);
            series.push(s);
        }

        // 停瀑 + 静置 + collect：断言回落（T1 保留窗到期 + 强制清退）
        stop.store(true, std::sync::atomic::Ordering::Relaxed);
        while let Some(res) = set.join_next().await {
            res.expect("actor task panic")
                .unwrap_or_else(|e| panic!("actor 错误: {e}"));
        }
        drop(watcher); // consumer 任务保活句柄随结构体结束
                       // 静置回落改轮询观察窗（最长 30s，回落到 slack 内提前收窗）：
                       // mimalloc 归还需 purge_delay 到期 + 分配段松弛，固定 300+600ms 在
                       // CI 高负载 runner 不足——purged 1223MB 在动（回收机制活）、commit
                       // 停在 302MB 未及回落即被断言（2026-11-06 CI 实证）。执法力不变：
                       // 30s 仍不回落 = 如实红。
        collect_now();
        let settle_deadline = tokio::time::Instant::now() + Duration::from_secs(30);
        let final_s = loop {
            tokio::time::sleep(Duration::from_millis(500)).await;
            collect_now();
            let s = sample();
            if s.commit_mb.saturating_sub(baseline.commit_mb) <= quiet_slack_mb
                || tokio::time::Instant::now() >= settle_deadline
            {
                break s;
            }
        };

        // ── 断言（结构性有界，非绝对数值）──
        let peak = series.iter().map(|s| s.commit_mb).max().unwrap_or(0);
        let third = series.len() / 3;
        let first_third_max = series[..third.max(1)]
            .iter()
            .map(|s| s.commit_mb)
            .max()
            .unwrap_or(0);
        let last_third_max = series[series.len().saturating_sub(third.max(1))..]
            .iter()
            .map(|s| s.commit_mb)
            .max()
            .unwrap_or(0);
        let purged_total = final_s.purged_mb.saturating_sub(baseline.purged_mb);
        println!(
            "storm result: baseline={baseline:?} peak={peak} final={final_s:?} \
             first_third_max={first_third_max} last_third_max={last_third_max} \
             purged_total_mb={purged_total}",
        );
        assert!(
            peak.saturating_sub(baseline.commit_mb) <= max_growth_mb,
            "峰值增长 {}/{}MB 超界——内存有界性被破坏",
            peak.saturating_sub(baseline.commit_mb),
            max_growth_mb
        );
        assert!(
            last_third_max <= first_third_max + 64,
            "后段峰值 {last_third_max} 比前段 {first_third_max} 高出超 64MB——单调增长（泄漏形态）",
        );
        assert!(
            final_s.commit_mb.saturating_sub(baseline.commit_mb) <= quiet_slack_mb,
            "静置回落失败：final {final_s:?} vs baseline {baseline:?}（ slack {quiet_slack_mb}MB）",
        );
    })
}

// ── 两档车道 ────────────────────────────────────────────────────────────────

/// 有界档：12s 预算，常规 rust-test 车道可跑（CI 每轮内存回归钉）。
#[test]
fn memory_super_storm_bounded() {
    let secs: u64 = std::env::var("MEM_STORM_SECS")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(12);
    run_storm(secs, 256, 192);
}

/// 全量档（手动）：90s 预算 + 宽松界。
#[test]
#[ignore = "压力测试：cargo test -p agentos-integration-tests --test memory_super_storm -- --ignored --nocapture"]
fn memory_super_storm_full() {
    let secs: u64 = std::env::var("MEM_STORM_SECS")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(90);
    run_storm(secs, 512, 384);
}
