//! SQLite 存储实现
//!
//! 实现 StorageBackend trait，使用 rusqlite 作为 SQLite 后端。
//! 消息层为 op 模型单格式：`message_slots`（纯索引槽位表）+ `blobs`（整条消息
//! 全文，内容寻址去重）——消息持久化走单一真值，无投影链路。
//!
//! [来源: docs/working/adr_engine_design.md §4.2]

/// 内核自有 per-run 易变键：checkpoint 瘦身与冷恢复合并跳过的内核侧键集（GAP-3）。
///
/// 这些键属于"本轮运行"而非"管道累计状态"——残留会在恢复时覆盖下一轮的
/// 新输入（重启后旧 user 消息被重放消费）。内核恢复侧（api server.rs）经
/// lib.rs 再导出消费同一份，双侧语义单一来源。
///
/// 插件语义的 per-run 键（tool_schemas/conversation_mode/thinking_strength/
/// core_type/core_plugin 等）**不在本表**——由插件 manifest `state.volatile_keys`
/// 声明（P1-4 声明化）。checkpoint 落档剥离集 = 本表 ∪ 声明键并集：api 收集
/// 全部 manifest 声明，经 `StorageBackend::set_declared_volatile_keys` 注入
/// store，与内核自有键在同一剥离点（save_checkpoint）消费；恢复合并侧
/// （api merge_recovered_scalars）消费同一并集跳过。新插件引入 per-run 键
/// 零内核改动，与 persistent_fields 声明化对称。
///
/// `ended` 与 `suspended` 同属 per-run 终止标志：post 阶段每轮由追踪插件写
/// `ended=true`，若残留进下一轮 initial_state，引擎
/// `execute_steps`/`execute_body` 见 ended 即短路——冷恢复（registry 丢失）
/// 后 run 秒终 completed、LLM 一次请求都不发。
///
/// `should_stop` 同属 per-run 控制键（控制状态键契约 ADR 2026-08-30）：
/// 插件终止请求在轮边界折算为 ended，残留会让冷恢复后的 run 立即终止。
/// `router.stop_reason` 是终止署名键，与 should_stop 同写同消——署名跨
/// run 残留会让恢复后的 run 按上一 run 的原因映射终态。
pub const VOLATILE_RUN_KEYS: &[&str] = &[
    "message",
    "input",
    "message_id",
    // run_id 是本轮取消/状态轮询的定位锚：快照恢复若不跳过，上一 run 的
    // run_id 会顶掉新 run 刚种的锚，停止/中断信号路由到旧 run 而失效。
    "run_id",
    "suspended",
    "ended",
    // run_status 是内核持有的实际状态（2026-09-03 双状态裁定）：每轮派发
    // 注入 running、收束由引擎按终态映射单点改写——快照恢复若不剥离，上一
    // run 的终态（如 completed）会顶掉本轮 running 起点，轮中可观测性失真。
    "run_status",
    "should_stop",
    "router.stop_reason",
    "_assistant_id_assigned",
    "_pending_message_ops",
    // agent_id 非派发注入键：执行身份 = 管道 state 持久键 agent.id（出生方经
    // state 透传，context_build/工具面消费），派发不注入。保留拦截——
    // 旧 checkpoint/轨迹恢复会把残留 agent_id 带回 state。
    "agent_id",
    // 「终态未落库」补偿标记（B5）：update_run_status 重试耗尽后的观测键，
    // 只做 G8 排空兜底的补写识别面——残留进下一轮 state 无意义且误导消费方。
    "terminal_persist_failed",
];

use std::sync::Arc;

use agentos_core::traits::{MessageQueryOpts, SessionListFilter, StorageBackend};
use agentos_core::types::{
    MessageRecord, PatchType, PendingInputRecord, PendingInputSource, PipelineRunInfo, RunRecord,
    RunStatus, SessionRecord, StorageError, TraceEntry, UserRecord,
};
use async_trait::async_trait;
use parking_lot::Mutex;
use rusqlite::{Connection, OptionalExtension};
use tracing::{error, info, warn};

/// SQLite schema 版本（PRAGMA user_version）。结构变更新增 MIGRATIONS 条目并递增。
const SCHEMA_VERSION: i64 = 1;

/// consumed_refresh_jtis 行保留窗口（秒）：与 refresh token 有效期上限对齐
/// （api auth REFRESH_TOKEN_TTL_SECS = 7 天）——token 本身过期后，其消费
/// 记录不再有吊销意义，消费写入时顺手清理窗口外旧行，表不无界增长。
const CONSUMED_REFRESH_JTI_TTL_SECS: u64 = 7 * 24 * 60 * 60;

/// traces 保留天数默认值（ADR 2026-09-11）：traces 每 step 追加一行、blobs
/// 内容寻址去重且不随消息/管道删除级联清理，无保留机制两表无界增长。
/// `AGENTOS_TRACE_RETENTION_DAYS` 可覆盖；0 = 禁用清扫。
pub const TRACE_RETENTION_DAYS_DEFAULT: u64 = 90;

/// 保留天数钳制上限（≈100 年）：env 误配超大值时把 cutoff 钳在 DateTime
/// 值域内，清扫任务不因 cutoff 计算溢出而 panic。
const TRACE_RETENTION_DAYS_MAX: u64 = 36_500;

/// traces 保留天数：`AGENTOS_TRACE_RETENTION_DAYS` > 默认
/// [`TRACE_RETENTION_DAYS_DEFAULT`]。解析失败回落默认（清扫是家政不是正确性
/// 路径，误配不阻断启动，与 `AGENTOS_MAX_CONCURRENT_RUNS` 同规）；0 = 禁用
/// （接线侧据此不启动清扫任务）；超出 [`TRACE_RETENTION_DAYS_MAX`] 按上限钳制。
pub fn trace_retention_days() -> u64 {
    std::env::var("AGENTOS_TRACE_RETENTION_DAYS")
        .ok()
        .and_then(|v| v.parse::<u64>().ok())
        .unwrap_or(TRACE_RETENTION_DAYS_DEFAULT)
        .min(TRACE_RETENTION_DAYS_MAX)
}

/// 单条迁移函数类型：把 schema 从版本 i 升到 i+1。
type MigrationFn = fn(&Connection) -> Result<(), StorageError>;

/// 有序迁移链：MIGRATIONS[i] 把 schema 从版本 i 升到 i+1。
///
/// v1 基线 = init() 的 DDL + 幂等迁移集（建表 IF NOT EXISTS / 补列存在性守护 /
/// 退役表 DROP，对 0.2 全期存量库幂等）。后续结构变更：追加 (描述, fn) 条目并
/// 递增 SCHEMA_VERSION；open 时只跑 user_version 之后的条目。数据库版本号高于
/// 本内核认知 = 新版本内核写出，显式拒绝（不猜、不静默降级——fail-closed）。
const MIGRATIONS: &[(&str, MigrationFn)] = &[];

/// SQLite 四表 DDL（建表脚本）
const DDL: &str = "
CREATE TABLE IF NOT EXISTS runs (
    run_id         TEXT PRIMARY KEY,
    config_hash    TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'running',
    tenant_id      TEXT NOT NULL,
    pipeline_id    TEXT,
    created_at     TEXT NOT NULL,
    ended_at       TEXT,
    current_branch TEXT NOT NULL,
    current_seq    INTEGER NOT NULL DEFAULT 0,
    metadata       TEXT
);
CREATE TABLE IF NOT EXISTS traces (
    trace_id      TEXT PRIMARY KEY,
    run_id        TEXT NOT NULL,
    branch_id     TEXT NOT NULL,
    seq_in_branch INTEGER NOT NULL,
    plugin_id     TEXT NOT NULL,
    patch_type    TEXT NOT NULL,
    patch_data    TEXT NOT NULL,
    tenant_id     TEXT NOT NULL DEFAULT 'default',
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_traces_branch_seq ON traces(branch_id, seq_in_branch);
-- 轨迹热查询按 run_id 集合 + tenant 过滤（get_step_traces_by_thread 经
-- message_slots 反查 run_id 集合后扫 traces），无此索引时该路径全表扫描。
CREATE INDEX IF NOT EXISTS idx_traces_run_tenant ON traces(run_id, tenant_id);
CREATE TABLE IF NOT EXISTS blobs (
    blob_id    TEXT PRIMARY KEY,
    mime_type  TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    data       BLOB NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS branches (
    branch_id     TEXT NOT NULL,
    run_id        TEXT NOT NULL,
    parent_branch TEXT,
    parent_seq    INTEGER,
    tenant_id     TEXT NOT NULL DEFAULT 'default',
    created_at    TEXT NOT NULL,
    PRIMARY KEY (branch_id, run_id)
);
-- 域2：session 标签夹层（对齐 0.1 SessionModel）。
-- 两域解耦：sessions 只持 pipeline_ids JSON 引用列表，不反向 join messages。
-- 会话只是聚合管道引用的标签，管道自治（消息/执行由管道自身负责）。
CREATE TABLE IF NOT EXISTS sessions (
    thread_id          TEXT PRIMARY KEY,
    title              TEXT,
    intent             TEXT,
    current_state      TEXT NOT NULL DEFAULT 'active',
    agent_id           TEXT,
    active_pipeline_id TEXT,
    pipeline_ids       TEXT,
    metadata           TEXT,
    tenant_id          TEXT NOT NULL DEFAULT 'default',
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    last_active_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_tenant ON sessions(tenant_id, updated_at DESC);
-- 域6：users（0.5.0 完整用户系统的最小持久化地基）。
-- 一用户一租户：tenant_id = user_id（admin 种子 = 'default'）。username 跨租户全局唯一。
-- password 存 argon2id 哈希（PHC 字符串）；存量明文行由内核启动迁移哈希化回写。
CREATE TABLE IF NOT EXISTS users (
    user_id       TEXT PRIMARY KEY,
    username      TEXT NOT NULL UNIQUE,
    password      TEXT NOT NULL,
    email         TEXT,
    role          TEXT NOT NULL DEFAULT 'user',
    tenant_id     TEXT NOT NULL DEFAULT 'default',
    created_at    TEXT NOT NULL,
    last_login_at TEXT,
    must_change_password INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_users_tenant ON users(tenant_id);
-- 域7：pipeline_sessions（会话↔管道映射）。
-- 一个会话下所有管道（主管道 + 子任务管道）都映射到同一 thread_id。
-- 删除会话时按 thread_id 一次查到全部 pipeline_id 级联清理，无需父子关系。
-- 写入时机：persist_run_start（每次管道开跑）+ create/update session（主管道兜底）。
CREATE TABLE IF NOT EXISTS pipeline_sessions (
    pipeline_id TEXT NOT NULL,
    thread_id   TEXT NOT NULL,
    tenant_id   TEXT NOT NULL DEFAULT 'default',
    created_at  TEXT NOT NULL,
    PRIMARY KEY (pipeline_id, tenant_id)
);
CREATE INDEX IF NOT EXISTS idx_ps_thread ON pipeline_sessions(thread_id, tenant_id);
-- 域8：pipeline_state（state 标量字段的实时快照）。
-- 除 messages 外，state 中需要跨轮保留/重建恢复的累计字段（如 track.total_tokens），
-- 每字段一行 upsert。冷启动重建时读出累计值喂回 state，插件自然累加。
-- 用完即弃的传送带字段（raw_tool_calls / tool_results / router.* 等）不进此表。
CREATE TABLE IF NOT EXISTS pipeline_state (
    pipeline_id  TEXT NOT NULL,
    field_key    TEXT NOT NULL,
    field_value  TEXT NOT NULL,
    tenant_id    TEXT NOT NULL DEFAULT 'default',
    updated_at   TEXT NOT NULL,
    PRIMARY KEY (pipeline_id, field_key, tenant_id)
);
-- 域9：pipeline_checkpoints（定期全量 state 快照，留档用）。
-- 每 N 步把当时完整 state 复制一份到此表。冷启动重建优先取最近 checkpoint（O(1) 基线），
-- 再回放其后 traces 增量。checkpoint 存全量（非 diff）——用存储换 O(1) 恢复速度，
-- 与 traces 增量化（省存储）配套。checkpoint 是状态表的留档副本，刻意冗余，N 步才产生一份。
CREATE TABLE IF NOT EXISTS pipeline_checkpoints (
    checkpoint_id  TEXT PRIMARY KEY,
    pipeline_id    TEXT NOT NULL,
    step_no        INTEGER NOT NULL,
    state_json     TEXT NOT NULL,
    tenant_id      TEXT NOT NULL DEFAULT 'default',
    created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cp_pipeline_step ON pipeline_checkpoints(pipeline_id, tenant_id, step_no DESC);
-- message_slots：op-based 消息槽位表（新模型，详见 docs/message_persistence_design.md）。
-- **纯索引表**（任务 7 收敛后）：行上零内容字段，消息全文（role/content/tool_calls/
-- reasoning_content/tool_result envelope）整体序列化成一个 blob 存 blobs 表（内容寻址去重）。
-- 读路径（get_slot_messages_by_pipeline / load_message_history / get_messages_by_pipeline）
-- join blobs 读时重建完整字段——存储收敛，接口形状不变。
-- 不变量：
--   * 主键 = (tenant_id, pipeline_id, seq)：seq 是稳定逻辑槽位（≠ 数组下标），删除留 gap；
--   * message_id = 整条消息规范化 hash（core::ids，排除 seq/_前缀字段）：内容变→id 变、seq 不变；
--   * 压缩：前段 summary 占最小 seq（id 变）、中段删 gap、后段 seq/id 不变、不顺延。
CREATE TABLE IF NOT EXISTS message_slots (
    tenant_id        TEXT NOT NULL DEFAULT 'default',
    pipeline_id      TEXT NOT NULL,
    seq              INTEGER NOT NULL,
    message_id       TEXT NOT NULL,
    blob_id          TEXT,
    run_id           TEXT,
    created_at       TEXT NOT NULL,
    PRIMARY KEY (tenant_id, pipeline_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_message_slots_pipeline_seq
    ON message_slots(pipeline_id, tenant_id, seq);
-- 域11：pipeline_pending_inputs（pending 输入队列，ADR-2026-08-26）。
-- 消息在入队→激活之间停留在此表：等待窗口内可修改/删除/清空，
-- 消费任务从表取参数执行（内容不被闭包捕获），重启后队列仍在（续跑）。
-- FIFO 序 = (created_at, id) 升序；消费瞬态 = 取出行并物理删除（无 status 列）。
CREATE TABLE IF NOT EXISTS pipeline_pending_inputs (
    id                TEXT PRIMARY KEY,
    pipeline_id       TEXT NOT NULL,
    tenant_id         TEXT NOT NULL,
    user_id           TEXT NOT NULL,
    content           TEXT NOT NULL,
    thread            TEXT NOT NULL,
    source            TEXT NOT NULL,
    agent_id          TEXT NOT NULL DEFAULT 'agentos',
    route_id          TEXT NOT NULL DEFAULT '',
    thinking_strength TEXT NOT NULL DEFAULT '',
    client_message_id TEXT NOT NULL DEFAULT '',
    execution_context TEXT,
    state_overlay     TEXT,
    created_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pending_pipeline
    ON pipeline_pending_inputs(tenant_id, pipeline_id, created_at, id);
-- consumed_refresh_jtis：refresh token 单次轮换消费账本（api auth D12）。
-- 跨进程重启持久判已消费（进程内记录重启即清，已消费 token 会复活）；
-- 行保留窗口 = refresh token 有效期上限 7 天（api auth REFRESH_TOKEN_TTL_SECS），
-- 消费写入时顺手清理窗口外旧行。
CREATE TABLE IF NOT EXISTS consumed_refresh_jtis (
    jti         TEXT PRIMARY KEY,
    consumed_at INTEGER NOT NULL
);
";

/// 退役表迁移处置：存在且有行 → `ALTER TABLE ... RENAME TO <name>_retired_<yyyymmddHHMMSS>`
/// 留档保全并 error 留痕（数据保全优先于自动清理——退役即盲删会把存量数据随升级
/// 静默抹掉，人工确认无用后再清理 _retired_ 表）；不存在或空表 → DROP（db_admin
/// 表清单与后端读写一一对应的既有契约不变）。
fn retire_table_preserve_rows(conn: &Connection, name: &str) -> Result<(), StorageError> {
    let exists: bool = conn
        .query_row(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = ?1",
            [name],
            |row| row.get::<_, i64>(0),
        )
        .map(|n| n > 0)?;
    if !exists {
        return Ok(());
    }
    let rows: i64 = conn.query_row(&format!("SELECT COUNT(*) FROM {name}"), [], |r| r.get(0))?;
    if rows == 0 {
        conn.execute(&format!("DROP TABLE {name}"), [])?;
        return Ok(());
    }
    let ts = chrono::Utc::now().format("%Y%m%d%H%M%S");
    let renamed = format!("{name}_retired_{ts}");
    conn.execute(&format!("ALTER TABLE {name} RENAME TO {renamed}"), [])?;
    error!(
        table = name,
        rows = rows,
        renamed = %renamed,
        "退役表含数据：已改名保全，不自动删除；确认无用后可人工 DROP 该 _retired_ 表"
    );
    Ok(())
}

/// 零兼容：检测 message_slots 旧 schema（含内容列，如 content_preview）并整表撤位重建。
///
/// 任务 7 收敛后 slots 是纯索引表；旧库的宽表（含 role/content_preview/tool_calls_json
/// 等内容列）数据格式不再支持——旧宽表经 [`retire_table_preserve_rows`] 处置（有行改名
/// `_retired_<ts>` 留档、空表 DROP），再按现行 DDL 重建纯索引表。
fn migrate_drop_legacy_message_slots(conn: &Connection) -> Result<(), StorageError> {
    // 行级读取失败必须显式留痕：filter_map(.ok()) 会把失败行静默丢掉，
    // 可能让残留旧列逃过检测、误判"无旧列"而跳过重建。
    let mut has_legacy_col = false;
    let mut stmt = conn.prepare("PRAGMA table_info(message_slots)")?;
    let rows = stmt.query_map([], |row| row.get::<_, String>(1))?;
    for r in rows {
        match r {
            Ok(col) => {
                if col == "content_preview" || col == "tool_calls_json" {
                    has_legacy_col = true;
                }
            }
            Err(e) => warn!(error = %e, "message_slots 列信息行读取失败，该行不计入旧 schema 检测"),
        }
    }
    if has_legacy_col {
        retire_table_preserve_rows(conn, "message_slots")?;
        warn!("message_slots 含旧内容列（零兼容），已按保全策略撤位并按现行 DDL 重建");
        conn.execute_batch(DDL)?;
    }
    Ok(())
}

/// 为旧库（建表时无 tenant_id 列）补加 tenant_id 列。
///
/// runs 表补 pipeline_id 列（GAP-1 统一：task = pipeline，按管道挂起/恢复
/// 需要 run 的管道归属）。幂等：列已存在时跳过。
fn migrate_add_run_pipeline_id(conn: &Connection) -> Result<(), StorageError> {
    let has = conn
        .prepare("SELECT COUNT(*) FROM pragma_table_info('runs') WHERE name='pipeline_id'")?
        .query_row([], |row| row.get::<_, i64>(0))?;
    if has == 0 {
        conn.execute("ALTER TABLE runs ADD COLUMN pipeline_id TEXT", [])?;
    }
    Ok(())
}

/// 为旧库（无 must_change_password 列的 users 表）补加该列。幂等：列已存在时跳过。
fn migrate_add_users_must_change_password(conn: &Connection) -> Result<(), StorageError> {
    let has = conn
        .prepare(
            "SELECT COUNT(*) FROM pragma_table_info('users') WHERE name='must_change_password'",
        )?
        .query_row([], |row| row.get::<_, i64>(0))?;
    if has == 0 {
        conn.execute(
            "ALTER TABLE users ADD COLUMN must_change_password INTEGER NOT NULL DEFAULT 0",
            [],
        )?;
    }
    Ok(())
}

/// 仅在列缺失时执行 `ALTER TABLE ... ADD COLUMN`，幂等。blob 表不加（内容寻址，靠上游归属）。
fn migrate_add_tenant_id(conn: &Connection) -> Result<(), StorageError> {
    for table in ["traces", "branches"] {
        // 行级读取失败必须显式留痕（不吞）：判断结果只取可读行，失败时保守跳过
        // ALTER——列存在性不明时不做可能自撞"duplicate column"的补列。
        let mut has_col = false;
        let mut stmt = conn.prepare(&format!("PRAGMA table_info({})", table))?;
        let rows = stmt.query_map([], |row| row.get::<_, String>(1))?;
        for r in rows {
            match r {
                Ok(col) => {
                    if col == "tenant_id" {
                        has_col = true;
                    }
                }
                Err(e) => {
                    warn!(table = table, error = %e, "列信息行读取失败，该行不计入 tenant_id 检测")
                }
            }
        }
        if !has_col {
            conn.execute(
                &format!(
                    "ALTER TABLE {} ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default'",
                    table
                ),
                [],
            )?;
        }
    }
    Ok(())
}

/// SQLite 四表存储实现。
///
/// 使用 `rusqlite::Connection`（线程安全包装在 `Arc<Mutex>` 中）。
/// 支持 WAL 模式以提高并发读写性能。
/// Clone 语义：浅拷 Arc 字段，多个 clone 共享同一连接与同一 DB 线程池。
/// DB 线程池派发转发 trait 方法时需要（'self' 借用无法 move 进闭包）。
#[derive(Clone)]
pub struct SqliteStore {
    conn: Arc<Mutex<Connection>>,
    /// 固定长命 DB 工作线程池：async 面的阻塞调用统一派发到此执行（见
    /// [`DedicatedDbPool`]）——固定线程持续复用 per-thread 堆，避免阻塞池
    /// 短命线程换手导致的堆废弃滞留。
    pool: Arc<DedicatedDbPool>,
    /// 插件声明 per-run 易变键并集（P1-4 声明化）：checkpoint 落档剥离集 =
    /// 内核自有键（VOLATILE_RUN_KEYS）∪ 本集。经 `set_declared_volatile_keys`
    /// 注入（api 从 manifest 声明收集，随派发刷新），缺省空集。
    declared_volatile_keys: Arc<parking_lot::RwLock<std::collections::HashSet<String>>>,
}

/// 判断 StorageError 是否为 SQLite 损坏类错误（可自愈重建）。
///
/// 覆盖 rusqlite 对坏库的典型报错：
/// - `database disk image is malformed`（SQLITE_CORRUPT=11，宿主 .kernel_02.log 即此报错）
/// - `file is not a database` / `file is encrypted or is not a database`（SQLITE_NOTADB=26）
/// - 其他包含 corrupt 字样的损坏描述
///
/// 权限/IO/磁盘满等非损坏错误**不**在此列，原样传播、不做静默降级。
fn is_corruption_error(e: &StorageError) -> bool {
    match e {
        StorageError::Database(msg) => {
            let lower = msg.to_lowercase();
            lower.contains("malformed")
                || lower.contains("not a database")
                || lower.contains("file is encrypted")
                || lower.contains("corrupt")
        }
        _ => false,
    }
}

/// 从槽位行的 blob 数据重建消息全文 JSON。
///
/// - `blob_id` 为 `None`：**合法缺失**（槽位本就无全文指针）→ 空对象，不算损坏；
/// - `blob_id` 有值但 blob 行缺失 / 非 UTF-8 / JSON 解析失败：**损坏** → 仍降级为
///   空对象（读路径行为不变），返回原因字符串供调用方聚合 warn（观测）。
fn decode_slot_message(
    blob_id: Option<&str>,
    data: Option<&[u8]>,
) -> (serde_json::Value, Option<String>) {
    let empty = || serde_json::Value::Object(Default::default());
    if blob_id.is_none() {
        return (empty(), None);
    }
    let Some(data) = data else {
        return (empty(), Some("blob 行缺失".to_string()));
    };
    let Some(text) = std::str::from_utf8(data).ok() else {
        return (empty(), Some("blob 内容非 UTF-8".to_string()));
    };
    match serde_json::from_str(text) {
        Ok(v) => (v, None),
        Err(e) => (empty(), Some(format!("blob JSON 解析失败: {e}"))),
    }
}

/// 从消息 JSON + 槽位元数据**读时重建** `MessageRecord`（纯索引行读路径共用）。
///
/// 纯索引行不再存内容列，所有字段从 blob 里的整条消息 JSON 提取——
/// 存储收敛，接口形状不变（前端/HTTP 读侧零改动）。
#[allow(clippy::too_many_arguments)]
fn slot_row_to_record(
    msg: &serde_json::Value,
    seq: i64,
    message_id: String,
    blob_id: Option<String>,
    created_at: String,
    pipeline_id: Option<String>,
    run_id: Option<String>,
) -> MessageRecord {
    let role = msg
        .get("role")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .to_string();
    let content = extract_content_string(msg);
    // 读时重建全文（存储收敛后无截断限制；字段名保留 preview 以稳接口形状）
    let content_preview = content.clone();
    // tool 结果状态：envelope 优先（结构化真相：tool_result.success/error），
    // content 前缀兜底（无 envelope 时的旧语义）。
    let (status, error) = if role == "tool" {
        let env = msg.get("tool_result");
        let env_success = env
            .and_then(|tr| tr.get("success"))
            .and_then(|v| v.as_bool());
        match env_success {
            Some(false) => {
                let err = env
                    .and_then(|tr| tr.get("error"))
                    .and_then(|v| v.as_str())
                    .map(String::from)
                    .or_else(|| content.strip_prefix("Error: ").map(String::from));
                (Some("failed".to_string()), err)
            }
            Some(true) => (Some("completed".to_string()), None),
            None => {
                if let Some(err_msg) = content.strip_prefix("Error: ") {
                    (Some("failed".to_string()), Some(err_msg.to_string()))
                } else {
                    (Some("completed".to_string()), None)
                }
            }
        }
    } else {
        (None, None)
    };
    MessageRecord {
        message_id,
        run_id: run_id.unwrap_or_default(),
        branch_id: String::new(),
        seq_in_branch: seq as u32,
        role,
        blob_id,
        content_preview: Some(content_preview),
        created_at,
        pipeline_id,
        tool_calls_json: msg.get("tool_calls").map(|tc| {
            serde_json::to_string(tc).expect("serde_json Value serialization is infallible")
        }),
        tool_call_id: msg
            .get("tool_call_id")
            .and_then(|v| v.as_str())
            .map(String::from),
        reasoning_content: msg
            .get("reasoning_content")
            .and_then(|v| v.as_str())
            .map(String::from),
        status,
        error,
        // envelope 随消息持久化（tool_result 字段），读时提取
        tool_result_json: msg.get("tool_result").map(|tr| {
            serde_json::to_string(tr).expect("serde_json Value serialization is infallible")
        }),
        // 自定义元数据随 blob 全文持久化，读时原样提取（user 消息的
        // client_message_id 幂等键契约，ADR 2026-08-21）
        metadata: msg.get("metadata").filter(|m| m.is_object()).cloned(),
    }
}

/// 从 messages 数组元素提取 content 的字符串表示。
///
/// content 可能是字符串（普通文本/工具结果）或数组（多 part：thinking/text 等）。
/// 数组形式时拼接所有 text part 的 text 字段，保持与前端渲染一致。
fn extract_content_string(msg: &serde_json::Value) -> String {
    match msg.get("content") {
        Some(serde_json::Value::String(s)) => s.clone(),
        // 多 part：拼接 text/thinking 的内容
        Some(serde_json::Value::Array(parts)) => parts
            .iter()
            .filter(|p| {
                matches!(
                    p.get("type").and_then(|v| v.as_str()),
                    Some("text" | "thinking")
                )
            })
            .filter_map(|p| p.get("text").and_then(|v| v.as_str()))
            .collect::<Vec<_>>()
            .join("\n"),
        _ => String::new(),
    }
}

/// 备份损坏的 SQLite 文件（含 -wal/-shm 伴生文件）为 `<src>.corrupt-<ts>`，保留现场供排查。
///
/// 返回本次备份的路径前缀 `<path>.corrupt-<ts>`（fail-closed 错误信息据此指引现场位置）。
///
/// 注意：WAL 模式下损坏可能落在 -wal/-shm 伴生文件中，因此三个文件一起处理。
/// 个别文件备份失败只 warn 不阻断——现场能保多少保多少，open 侧无论备份成败
/// 都会在默认路径 fail-closed 拒启，不会在无现场的情况下继续。
fn backup_corrupt_files(path: &str) -> String {
    let ts = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| format!("{}.{}", d.as_secs(), d.subsec_nanos()))
        .unwrap_or_else(|_| "unknown".to_string());
    let prefix = format!("{path}.corrupt-{ts}");
    for suffix in ["", "-wal", "-shm"] {
        let src = format!("{path}{suffix}");
        let dst = format!("{prefix}{suffix}");
        if std::path::Path::new(&src).exists() {
            match std::fs::rename(&src, &dst) {
                Ok(()) => warn!(
                    from = %src,
                    to = %dst,
                    "损坏的 SQLite 文件已备份保留现场"
                ),
                Err(e) => warn!(
                    from = %src,
                    error = %e,
                    "损坏的 SQLite 文件备份失败（现场不完整，启动仍将中止）"
                ),
            }
        }
    }
    prefix
}

/// 解析 traces.patch_type 字符串为 PatchType。
///
/// 未知值静默归 StateUpdate 是既有语义（新引擎写入方全部命中已知值），
/// 但必须留痕——未知值 warn 暴露，避免冷恢复回放对新增类型静默降级。
fn parse_patch_type(patch_type: &str, plugin_id: &str) -> PatchType {
    match patch_type {
        "state_update" => PatchType::StateUpdate,
        "route_signal" => PatchType::RouteSignal,
        "error" => PatchType::Error,
        "lifecycle" => PatchType::Lifecycle,
        "rollback" => PatchType::Rollback,
        _ => {
            warn!(
                plugin_id = %plugin_id,
                patch_type = %patch_type,
                "traces 出现未知 patch_type，按 StateUpdate 处理（写入方新增类型需登记）",
            );
            PatchType::StateUpdate
        }
    }
}

/// runs.status 文本 → RunStatus：未知值 warn 留痕后按 Running 处理（读取
/// 路径不可 fail，但写入方新增状态必须登记——与 parse_patch_type 同标准）。
fn parse_run_status(status: &str, run_id: &str) -> RunStatus {
    match status {
        "running" => RunStatus::Running,
        "suspended" => RunStatus::Suspended,
        "completed" => RunStatus::Completed,
        "failed" => RunStatus::Failed,
        "cancelled" => RunStatus::Cancelled,
        _ => {
            warn!(
                run_id = %run_id,
                status = %status,
                "runs 出现未知 status，按 Running 处理（写入方新增状态需登记）",
            );
            RunStatus::Running
        }
    }
}

/// runs.metadata JSON 文本 → Option<Value>：损坏 JSON warn 留痕后按缺失处理，
/// 不与"无 metadata"静默混同（审批挂起凭据 pending_interaction_request_id
/// 落在此列，静默丢弃会让挂起恢复链查不到 run 且无任何痕迹）。
fn parse_run_metadata(metadata_str: Option<String>, run_id: &str) -> Option<serde_json::Value> {
    let raw = metadata_str?;
    match serde_json::from_str(&raw) {
        Ok(v) => Some(v),
        Err(e) => {
            warn!(
                run_id = %run_id,
                error = %e,
                "runs metadata JSON 损坏，按缺失处理（挂起恢复凭据可能丢失）",
            );
            None
        }
    }
}

impl SqliteStore {
    /// 在指定路径创建 SQLite 数据库并初始化表结构。
    ///
    /// 损坏 fail-closed：打开/初始化失败且错误为 SQLite 损坏类（malformed / not a
    /// database / corrupt / file is encrypted，如进程异常退出或磁盘故障留下的坏库）时，
    /// 先把损坏文件（含 -wal/-shm 伴生文件）备份为 `<path>.corrupt-<ts>` 保留现场，
    /// 然后返回明确错误中止启动——库内是 runs/traces/messages 等审计型账本，默认
    /// 静默清零重建属高危操作。环境变量 `AGENTOS_DB_AUTO_REBUILD=1` 显式选择自动
    /// 重建：备份后重建空库继续启动（原数据只在 .corrupt-* 备份中）。
    /// 其他错误（权限、IO 等）原样传播，不做静默降级。
    pub fn open(path: &str) -> Result<Self, StorageError> {
        match Self::open_inner(path) {
            Ok(store) => Ok(store),
            Err(e) if is_corruption_error(&e) => {
                let backup_prefix = backup_corrupt_files(path);
                if std::env::var("AGENTOS_DB_AUTO_REBUILD").ok().as_deref() != Some("1") {
                    return Err(StorageError::Database(format!(
                        "SQLite 数据库损坏，已备份现场并中止启动（默认 fail-closed，不自动重建空库——\
                         审计账本静默清零属高危）。损坏文件备份于 {backup_prefix}*.corrupt-*。\
                         恢复指引：离线排查该备份并恢复数据；确认可弃数据后设 \
                         AGENTOS_DB_AUTO_REBUILD=1 重启，将自动重建空库继续。库路径：{path}。\
                         原始错误：{e}"
                    )));
                }
                warn!(
                    path = %path,
                    error = %e,
                    "SQLite 数据库损坏，AGENTOS_DB_AUTO_REBUILD=1：自动备份并重建新库（原数据保留在 .corrupt-* 备份中）"
                );
                Self::open_inner(path)
            }
            Err(e) => Err(e),
        }
    }

    fn open_inner(path: &str) -> Result<Self, StorageError> {
        let conn = Connection::open(path)?;
        Self::init(&conn)?;
        Ok(Self {
            conn: Arc::new(Mutex::new(conn)),
            pool: Arc::new(DedicatedDbPool::spawn()?),
            declared_volatile_keys: Arc::new(parking_lot::RwLock::new(Default::default())),
        })
    }

    /// 创建内存数据库（用于测试）。
    pub fn open_memory() -> Result<Self, StorageError> {
        let conn = Connection::open_in_memory()?;
        Self::init(&conn)?;
        Ok(Self {
            conn: Arc::new(Mutex::new(conn)),
            pool: Arc::new(DedicatedDbPool::spawn()?),
            declared_volatile_keys: Arc::new(parking_lot::RwLock::new(Default::default())),
        })
    }

    fn init(conn: &Connection) -> Result<(), StorageError> {
        conn.execute_batch("PRAGMA journal_mode=WAL;")?;
        // H9 迁移链：user_version 是版本权威。高于本内核认知 = 新版本内核写出，
        // 继续写会静默破坏不认识的 schema——显式拒绝（fail-closed，不猜）。
        let current_version: i64 = conn.query_row("PRAGMA user_version", [], |r| r.get(0))?;
        // 投影表退役（不留两套真值）：执行记录/会话消耗账本
        // 由 messages 真值派生读路径替代（调试中心执行记录/LLM 请求页），记忆面归
        // hindsight 自持存储；dynamic_tools 同样退役——动态注册的工具是 state 域
        // 数据不落内核（跨重启由插件自持 state/config 重建）。
        // 四表零生产者；必须在 DDL 之前处置——存量残留表结构与现行 DDL 的
        // CREATE INDEX 不兼容会直接炸 init。处置带保全：有行改名 `_retired_<ts>`
        // 留档（error 留痕，人工清理），空表 DROP。
        for retired in [
            "execution_records",
            "pipeline_run_summaries",
            "memory",
            "dynamic_tools",
        ] {
            retire_table_preserve_rows(conn, retired)?;
        }
        conn.execute_batch(DDL)?;
        migrate_drop_legacy_message_slots(conn)?;
        // 零兼容：0.2 消息真值 = message_slots ⨝ blobs（见文件头注释），旧 messages
        // 投影表已退役。存量库残留的该表按保全策略处置（有行改名 `_retired_<ts>`
        // 留档、空表 DROP），保证 db_admin 表清单与后端实际读写一一对应。
        retire_table_preserve_rows(conn, "messages")?;
        migrate_add_tenant_id(conn)?;
        migrate_add_run_pipeline_id(conn)?;
        migrate_add_users_must_change_password(conn)?;
        if current_version > SCHEMA_VERSION {
            return Err(StorageError::Database(format!(
                "数据库 schema 版本 {} 高于本内核支持的 {}（由更新版本的内核写出）——拒绝打开以防止静默破坏；请升级内核或迁移数据",
                current_version, SCHEMA_VERSION
            )));
        }
        for (name, migrate) in MIGRATIONS.iter().skip(current_version.max(0) as usize) {
            migrate(conn)?;
            info!("SQLite schema migrated: {}", name);
        }
        conn.execute_batch(&format!("PRAGMA user_version = {SCHEMA_VERSION};"))?;
        info!("SQLite four-table store initialized (schema v{SCHEMA_VERSION})");
        Ok(())
    }

    // ── runs 表操作 ──────────────────────────────────────────

    /// 创建运行实例。
    ///
    /// runs 行与 branches main 行同一事务落库：各自 autocommit 时进程在两条
    /// INSERT 之间被截断会留下无分支的 run（轨迹读路径按分支定位，半态不可恢复）。
    pub fn create_run(
        &self,
        run_id: &str,
        config_hash: &str,
        tenant_id: &str,
    ) -> Result<(), StorageError> {
        let conn = self.conn.lock();
        let now = chrono::Utc::now().to_rfc3339();
        if let Err(e) = conn.execute_batch("BEGIN") {
            return Err(StorageError::Database(format!("begin tx: {e}")));
        }
        let result = Self::create_run_in_tx(&conn, run_id, config_hash, tenant_id, &now);
        match result {
            Ok(()) => {
                if let Err(e) = conn.execute_batch("COMMIT") {
                    let _ = conn.execute_batch("ROLLBACK");
                    return Err(StorageError::Database(format!("commit tx: {e}")));
                }
                Ok(())
            }
            Err(e) => {
                let _ = conn.execute_batch("ROLLBACK");
                Err(e)
            }
        }
    }

    /// 事务体：runs + branches main 两行写入（调用方负责 BEGIN/COMMIT/ROLLBACK，
    /// 语义对齐 apply_messages_ops_in_tx：出错整批回滚，不残留部分写入）。
    fn create_run_in_tx(
        conn: &Connection,
        run_id: &str,
        config_hash: &str,
        tenant_id: &str,
        now: &str,
    ) -> Result<(), StorageError> {
        conn.execute(
            "INSERT INTO runs (run_id, config_hash, status, tenant_id, created_at, current_branch, current_seq) VALUES (?1, ?2, 'running', ?3, ?4, 'main', 0)",
            rusqlite::params![run_id, config_hash, tenant_id, now],
        )?;
        // 创建主分支（与 run 同租户）
        conn.execute(
            "INSERT INTO branches (branch_id, run_id, tenant_id, created_at) VALUES ('main', ?1, ?2, ?3)",
            rusqlite::params![run_id, tenant_id, now],
        )?;
        Ok(())
    }

    /// G8 优雅重启排空：把所有 `running` 的 run 标记 `suspended`（不设 ended_at
    /// ——run 未结束只是挂起，重启后 resume 续跑）。返回受影响行数。
    /// 与 reap_orphan_runs（崩溃清扫→failed）语义不同：这是**主动排空**，
    /// run 处于可恢复状态。
    pub fn suspend_running_runs(&self) -> Result<u64, StorageError> {
        let conn = self.conn.lock();
        let rows = conn.execute(
            "UPDATE runs SET status = 'suspended' WHERE status = 'running'",
            [],
        )?;
        Ok(rows as u64)
    }

    /// 启动时清扫孤儿 run（B2）：进程上次崩溃留下的 `status='running'` 的 run
    /// 标记为 `failed` 并补 `ended_at`（未补过才补），让历史/会话状态不悬空。
    /// 已结束（completed/failed/suspended）的 run 不受影响。返回被清扫的行数。
    /// 全租户清扫：注册用户一用户一租户，按租户过滤会让非 default 租户的
    /// 孤儿 run 永远卡 running——清扫是崩溃家政，不属于任何租户的数据边界。
    ///
    /// 同步修复内核持有的 `run_status` 状态投影（pipeline_state 表）：崩溃 run
    /// 的收束投影没跑过，冷读行缺 run_status（或残留轮中写入的 running）会让
    /// 消费方（/pipelines/state 三源推断、插件 reconcile）把死管道猜成 running
    /// （BUG-2 幽灵执行中）。规则：缺失/残留 running → failed；已有终态（更早
    /// 正常收束的真值）不覆盖，最新 run 状态的纠偏归读面 overlay。
    pub fn reap_orphan_runs(&self) -> Result<u64, StorageError> {
        let conn = self.conn.lock();
        let now = chrono::Utc::now().to_rfc3339();
        // 先收集被清扫 run 的 (pipeline_id, tenant_id) 坐标（UPDATE 前抓，
        // 清扫后 status 不再是 running）
        let victims: Vec<(String, String)> = {
            let mut stmt = conn.prepare(
                "SELECT DISTINCT pipeline_id, tenant_id FROM runs \
                 WHERE status = 'running' AND pipeline_id IS NOT NULL AND pipeline_id != ''",
            )?;
            let rows = stmt.query_map([], |row| {
                Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
            })?;
            rows.collect::<Result<Vec<_>, _>>()?
        };
        let rows = conn.execute(
            "UPDATE runs SET status = 'failed', ended_at = COALESCE(ended_at, ?1) \
             WHERE status = 'running'",
            rusqlite::params![now],
        )?;
        for (pipeline_id, tenant_id) in victims {
            let current: Option<String> = conn
                .query_row(
                    "SELECT field_value FROM pipeline_state \
                     WHERE pipeline_id = ?1 AND field_key = 'run_status' AND tenant_id = ?2",
                    rusqlite::params![pipeline_id, tenant_id],
                    |row| row.get(0),
                )
                .optional()?;
            let needs_fix = match current {
                None => true,
                Some(raw) => {
                    serde_json::from_str::<serde_json::Value>(&raw)
                        .ok()
                        .and_then(|v| v.as_str().map(|s| s.to_string()))
                        .as_deref()
                        == Some("running")
                }
            };
            if !needs_fix {
                continue;
            }
            let value = serde_json::json!("failed").to_string();
            conn.execute(
                "INSERT INTO pipeline_state (pipeline_id, field_key, field_value, tenant_id, updated_at) \
                 VALUES (?1, 'run_status', ?2, ?3, ?4) \
                 ON CONFLICT(pipeline_id, field_key, tenant_id) DO UPDATE SET field_value = ?2, updated_at = ?4",
                rusqlite::params![pipeline_id, value, tenant_id, now],
            )?;
        }
        Ok(rows as u64)
    }

    /// 更新 run 的 metadata 字段（JSON 文本，整体替换）。
    ///
    /// 用于 approval/human_interaction 插件 suspend run 时写入
    /// `pending_interaction_request_id` + `suspend_branch_id` + `suspend_seq`，
    /// 后续 `find_suspended_run_by_request_id` 按此查找并还原 resume 凭据。
    pub fn set_run_metadata(
        &self,
        run_id: &str,
        metadata: &serde_json::Value,
    ) -> Result<(), StorageError> {
        let conn = self.conn.lock();
        conn.execute(
            "UPDATE runs SET metadata = ?1 WHERE run_id = ?2",
            rusqlite::params![metadata.to_string(), run_id],
        )?;
        Ok(())
    }

    /// 按 `pending_interaction_request_id` 查找 Suspended run。
    ///
    /// 遍历所有 status='suspended' 的 run，解析 metadata，返回首个
    /// `pending_interaction_request_id` 匹配的 RunRecord。用于
    /// `dispatch_interaction_response` 根据 request_id 定位被挂起的 run。
    pub fn find_suspended_run_by_request_id(
        &self,
        request_id: &str,
    ) -> Result<Option<RunRecord>, StorageError> {
        let conn = self.conn.lock();
        let mut stmt = conn.prepare(
            "SELECT run_id, config_hash, status, tenant_id, created_at, ended_at, \
                 current_branch, current_seq, metadata \
                 FROM runs WHERE status = 'suspended'",
        )?;
        let rows = stmt.query_map([], |row| {
            let metadata_str: Option<String> = row.get(8)?;
            Ok((
                row.get::<_, String>(0)?,         // run_id
                row.get::<_, String>(1)?,         // config_hash
                row.get::<_, String>(3)?,         // tenant_id
                row.get::<_, String>(4)?,         // created_at
                row.get::<_, Option<String>>(5)?, // ended_at
                row.get::<_, String>(6)?,         // current_branch
                row.get::<_, i64>(7)? as u32,     // current_seq
                metadata_str,
            ))
        })?;

        for row in rows {
            let (
                run_id,
                config_hash,
                tenant_id,
                created_at,
                ended_at,
                current_branch,
                current_seq,
                metadata_str,
            ) = row?;

            if let Some(ref meta_str) = metadata_str {
                // metadata JSON 腐败的 run 显式留痕后跳过——静默 continue 会让
                // 挂起 run 因不可读元数据而永远找不到。
                match serde_json::from_str::<serde_json::Value>(meta_str) {
                    Ok(meta) => {
                        if meta
                            .get("pending_interaction_request_id")
                            .and_then(|v| v.as_str())
                            == Some(request_id)
                        {
                            return Ok(Some(RunRecord {
                                run_id,
                                config_hash,
                                status: RunStatus::Suspended,
                                tenant_id,
                                created_at,
                                ended_at,
                                current_branch,
                                current_seq,
                                metadata: Some(meta),
                            }));
                        }
                    }
                    Err(e) => {
                        warn!(run_id = %run_id, error = %e, "runs.metadata JSON 腐败，跳过该 run 的 request_id 匹配")
                    }
                }
            }
        }
        Ok(None)
    }

    /// 管道运行快照列表（统一管道管理查询，`GET /api/v1/pipelines/runs`）。
    ///
    /// runs × message_slots × pipeline_sessions 三表联结：
    /// run → pipeline 映射经 message_slots.run_id（op-based 落槽时写入），pipeline → 会话
    /// 经 pipeline_sessions。消耗账本真值在 state 的 track.total_tokens
    /// （pipeline_run_summaries 投影已退役）。
    /// 无消息槽的 run（旧引擎 start_run 占位/孤儿）被过滤——只呈现真实执行的管道。
    /// 按 started_at（created_at）倒序；`status` 传 None 返回全部状态；limit 由调用方给。
    pub fn list_pipelines_inner(
        &self,
        tenant_id: &str,
        status: Option<&str>,
        limit: u32,
    ) -> Result<Vec<PipelineRunInfo>, StorageError> {
        let conn = self.conn.lock();
        let mut stmt = conn.prepare(
            "SELECT r.run_id, r.status, r.created_at, r.ended_at, \
                    ms.pipeline_id, ps.thread_id \
             FROM runs r \
             LEFT JOIN (SELECT run_id, MAX(pipeline_id) AS pipeline_id \
                        FROM message_slots \
                        WHERE pipeline_id IS NOT NULL \
                        GROUP BY run_id) ms ON ms.run_id = r.run_id \
             LEFT JOIN pipeline_sessions ps \
                    ON ps.pipeline_id = ms.pipeline_id AND ps.tenant_id = ?1 \
             WHERE r.tenant_id = ?1 \
               AND ms.pipeline_id IS NOT NULL \
               AND (?2 IS NULL OR r.status = ?2) \
             ORDER BY r.created_at DESC \
             LIMIT ?3",
        )?;
        let rows = stmt.query_map(rusqlite::params![tenant_id, status, limit], |row| {
            let status_str: String = row.get(1)?;
            Ok(PipelineRunInfo {
                run_id: row.get(0)?,
                status: match status_str.as_str() {
                    "suspended" => RunStatus::Suspended,
                    "completed" => RunStatus::Completed,
                    "failed" => RunStatus::Failed,
                    "cancelled" => RunStatus::Cancelled,
                    _ => RunStatus::Running,
                },
                started_at: row.get(2)?,
                ended_at: row.get(3)?,
                pipeline_id: row.get(4)?,
                thread_id: row.get(5)?,
            })
        })?;
        let mut out = Vec::new();
        for row in rows {
            out.push(row?);
        }
        Ok(out)
    }

    /// 存储 BLOB 数据（内容寻址去重）。
    pub fn store_blob(&self, data: &[u8], mime_type: &str) -> Result<String, StorageError> {
        let blob_id = agentos_core::ids::compute_blob_id(data);
        let conn = self.conn.lock();
        // 先检查是否已存在（去重）
        let exists: bool = conn
            .query_row(
                "SELECT COUNT(*) > 0 FROM blobs WHERE blob_id = ?1",
                rusqlite::params![blob_id],
                |row| row.get(0),
            )
            .unwrap_or(false);
        if !exists {
            let now = chrono::Utc::now().to_rfc3339();
            conn.execute(
                "INSERT INTO blobs (blob_id, mime_type, size_bytes, data, created_at) VALUES (?1, ?2, ?3, ?4, ?5)",
                rusqlite::params![blob_id, mime_type, data.len() as i64, data, now],
            )?;
        }
        Ok(blob_id)
    }

    // ── 域10：分层持久化投影（messages 增量对齐 + 标量快照 + checkpoint）────
    // 设计：引擎 merge state_updates 时，对 messages（系统字段）走 project_messages
    // 增量对齐（索引比对，追加 O(1)）；对插件 manifest 声明的 persistent_fields 走
    // upsert_state_field（标量快照）；传送带字段不投影。checkpoint 每 N 步复制完整 state。

    /// 应用槽位 ops 到 message_slots 表（op-based 新模型单写入器）。
    ///
    /// 内核只"按插件来"：把插件对 `state["messages"]` 的改动落表，不做 diff、不生成身份。
    /// 详见 `docs/message_persistence_design.md`。**只有两个原语**：
    ///
    /// - `{"op":"set","seq":N,"msg":<obj|null>}`：统一 append / modify / delete。
    ///   - `msg` 为对象 → 写槽位 N（append=写新末槽 max+1、modify=写已存在槽；
    ///     内容变 → `message_id` 变、`seq` 不变）
    ///   - `msg` 为 null/缺省 → 清空槽位 N（delete=留 gap，后段不动）
    /// - `{"op":"insert","at":N,"msg":{...}}`：在位置 N 插入槽位，`seq>=N` 的后段顺延 +1
    ///   （后段 `message_id` 不变，仅 `seq+1`）。
    ///
    /// `message_id = agentos_core::ids::compute_message_id(msg)`（整消息规范化 hash，与 seq 解耦）。
    pub fn apply_messages_ops_to_table(
        &self,
        pipeline_id: &str,
        tenant_id: &str,
        ops: &[serde_json::Value],
    ) -> Result<(), StorageError> {
        let conn = self.conn.lock();
        let pid = pipeline_id.to_string();
        let now = chrono::Utc::now().to_rfc3339();

        // GAP-3：整批一个显式事务——blob 与 slot 两条写入要么都提交要么都回滚。
        // 各自 autocommit 时，进程在两条语句之间被截断会留下「slot 落了、
        // blob_id NULL」的半态（e2e 消息正文丢失）。
        // 语义等价 rusqlite 事务：出错回滚整批，不残留部分写入。
        if let Err(e) = conn.execute_batch("BEGIN") {
            return Err(StorageError::Database(format!("begin tx: {e}")));
        }
        let result = self.apply_messages_ops_in_tx(&conn, tenant_id, &pid, ops, &now);
        match result {
            Ok(()) => {
                if let Err(e) = conn.execute_batch("COMMIT") {
                    let _ = conn.execute_batch("ROLLBACK");
                    return Err(StorageError::Database(format!("commit tx: {e}")));
                }
                Ok(())
            }
            Err(e) => {
                let _ = conn.execute_batch("ROLLBACK");
                Err(e)
            }
        }
    }

    /// 事务体：逐 op 写表（原逻辑抽出，调用方负责 BEGIN/COMMIT/ROLLBACK）。
    fn apply_messages_ops_in_tx(
        &self,
        conn: &Connection,
        tenant_id: &str,
        pid: &str,
        ops: &[serde_json::Value],
        now: &str,
    ) -> Result<(), StorageError> {
        for op in ops {
            let kind = op.get("op").and_then(|v| v.as_str()).unwrap_or("");
            match kind {
                "set" => {
                    let Some(seq) = op.get("seq").and_then(|v| v.as_u64()) else {
                        continue;
                    };
                    match op.get("msg") {
                        Some(msg) if msg.is_object() => {
                            let run_id = op.get("_run_id").and_then(|v| v.as_str());
                            // A1：op 上的内部字段 `_message_id`（内核注入的流式 message_id）
                            // 优先作 record_id；缺省回退内容指纹。
                            let preferred_id = op.get("_message_id").and_then(|v| v.as_str());
                            self.write_slot_to_table_locked(
                                conn,
                                tenant_id,
                                pid,
                                seq as i64,
                                msg,
                                preferred_id,
                                run_id,
                                now,
                            )?;
                        }
                        _ => {
                            // 清空槽位（留 gap），不动其它槽位 → 后段 seq/id 不变。
                            conn.execute(
                                "DELETE FROM message_slots WHERE tenant_id=?1 AND pipeline_id=?2 AND seq=?3",
                                rusqlite::params![tenant_id, pid, seq as i64],
                            )?;
                        }
                    }
                }
                "insert" => {
                    let Some(at) = op.get("at").and_then(|v| v.as_u64()) else {
                        continue;
                    };
                    let Some(msg) = op.get("msg") else {
                        continue;
                    };
                    let at = at as i64;
                    // 复合 PK 下直接 `seq=seq+1` 会瞬态碰撞（更新途中两行抢同一 PK），
                    // 用大偏移两步法避开：① 受影响行 seq+=BIG ② 再 -=(BIG-1) → 净 +1，
                    // 两步内任意时刻 PK 都不冲突。
                    const BIG: i64 = 1_000_000_000;
                    conn.execute(
                        "UPDATE message_slots SET seq = seq + ?1 \
                         WHERE tenant_id=?2 AND pipeline_id=?3 AND seq >= ?4",
                        rusqlite::params![BIG, tenant_id, pid, at],
                    )?;
                    conn.execute(
                        "UPDATE message_slots SET seq = seq - ?1 \
                         WHERE tenant_id=?2 AND pipeline_id=?3 AND seq >= ?4",
                        rusqlite::params![BIG - 1, tenant_id, pid, BIG],
                    )?;
                    self.write_slot_to_table_locked(
                        conn, tenant_id, pid, at, msg, None, None, now,
                    )?;
                }
                _ => {
                    // 未知 op 忽略（前向兼容）。
                }
            }
        }
        Ok(())
    }

    /// 把单条消息写到指定槽位（`INSERT … ON CONFLICT(pipeline_id,seq) DO UPDATE`）。
    /// 供 `apply_messages_ops_to_table` 的 `set`/`insert` 复用。调用方须已持有 `conn` 锁。
    ///
    /// 纯索引行（任务 7）：整条消息序列化成一个 blob（内容寻址去重），行上只存
    /// (seq, message_id, blob_id)——零内容列。读路径 join blobs 读时重建。
    // 技术债（同 ROADMAP 已知技术债表 PLR091x 治理方式）：8 参内部函数，
    // 拆分参数结构体的改造留待 engine 收尾时统一做。
    #[allow(clippy::too_many_arguments)]
    fn write_slot_to_table_locked(
        &self,
        conn: &Connection,
        tenant_id: &str,
        pid: &str,
        seq: i64,
        msg: &serde_json::Value,
        preferred_id: Option<&str>,
        run_id: Option<&str>,
        now: &str,
    ) -> Result<(), StorageError> {
        // 整条消息（含 role/content/tool_calls/reasoning_content/tool_result envelope）
        // 序列化进 blob——消息是不可变值，全文唯一存储在 blobs。
        let msg_json =
            serde_json::to_string(msg).expect("serde_json Value serialization is infallible");
        let (blob_id, _) = self.ensure_blob_locked(conn, &msg_json)?;
        // A1：内核注入的流式 message_id 优先（流式占位与 DB record_id 对齐），
        // 缺省回退内容指纹。preferred_id 只影响 record_id，blob 全文不含它。
        let message_id = preferred_id
            .filter(|s| !s.is_empty())
            .map(|s| s.to_string())
            .unwrap_or_else(|| agentos_core::ids::compute_message_id(msg));
        conn.execute(
            "INSERT INTO message_slots
               (tenant_id, pipeline_id, seq, message_id, blob_id, run_id, created_at)
             VALUES (?1,?2,?3,?4,?5,?6,?7)
             ON CONFLICT(tenant_id, pipeline_id, seq) DO UPDATE SET
               message_id=excluded.message_id, blob_id=excluded.blob_id,
               run_id=excluded.run_id, created_at=excluded.created_at",
            rusqlite::params![tenant_id, pid, seq, message_id, blob_id, run_id, now,],
        )?;
        Ok(())
    }

    /// 读 `message_slots` 表（支持 before/after_sequence 游标与 limit，升序返回）。
    ///
    /// 窗口锚定方向（ADR 2026-08-23 消息窗口分页语义）：
    /// - `after_sequence` 锚定：游标之后**前** limit 条（ASC+LIMIT，断线补漏）；
    /// - 无 after 游标（首屏 / before 游标翻页）：limit 取**最新** limit 条
    ///   （尾锚定窗口：SQL DESC+LIMIT 取回后反转为 ASC）。
    ///   恒 ASC+LIMIT 会让首屏拿到最老 N 条，会话超 N 条时刷新回滚到
    ///   早期窗口，其后消息全部"消失"；before 翻页则跳空中间段留永久空洞。
    ///   纯索引行 join blobs **读时重建** `MessageRecord`（role/preview/tool_calls 等
    ///   全部从消息 JSON 提取）——存储收敛，接口形状不变。
    pub fn get_slot_messages_by_pipeline(
        &self,
        pipeline_id: &str,
        tenant_id: &str,
        opts: MessageQueryOpts,
    ) -> Result<Vec<MessageRecord>, StorageError> {
        let conn = self.conn.lock();
        let mut sql = String::from(
            "SELECT s.seq, s.message_id, s.blob_id, s.created_at, s.pipeline_id, s.run_id, b.data \
             FROM message_slots s LEFT JOIN blobs b ON s.blob_id = b.blob_id \
             WHERE s.pipeline_id = ?1 AND s.tenant_id = ?2",
        );
        let mut idx = 3;
        if opts.before_sequence.is_some() {
            sql.push_str(&format!(" AND s.seq < ?{}", idx));
            idx += 1;
        }
        if opts.after_sequence.is_some() {
            sql.push_str(&format!(" AND s.seq > ?{}", idx));
            idx += 1;
        }
        // 尾锚定窗口：无 after 游标 + limit → DESC 取最新 N 条（结果统一反转为 ASC）；
        // after 游标 → 头锚定（游标之后前 N 条，ASC+LIMIT 即正确）。
        // 确定性第二排序键与锚定方向一致（DESC 时同样 DESC，反转后仍为 ASC 稳定序）。
        let tail_anchored = opts.after_sequence.is_none() && opts.limit.is_some();
        sql.push_str(if tail_anchored {
            " ORDER BY s.seq DESC, s.created_at DESC, s.message_id DESC"
        } else {
            " ORDER BY s.seq ASC, s.created_at ASC, s.message_id ASC"
        });
        if opts.limit.is_some() {
            sql.push_str(&format!(" LIMIT ?{}", idx));
        }

        let mut stmt = conn.prepare(&sql)?;
        let mut params: Vec<Box<dyn rusqlite::ToSql>> = vec![
            Box::new(pipeline_id.to_string()),
            Box::new(tenant_id.to_string()),
        ];
        if let Some(before) = opts.before_sequence {
            params.push(Box::new(before as i64));
        }
        if let Some(after) = opts.after_sequence {
            params.push(Box::new(after as i64));
        }
        if let Some(limit) = opts.limit {
            params.push(Box::new(limit as i64));
        }
        let param_refs: Vec<&dyn rusqlite::ToSql> = params.iter().map(|p| p.as_ref()).collect();

        // blob 解析：区分合法缺失（blob_id NULL）与损坏（有 blob_id 但读不出/解析失败）。
        // 损坏行仍降级为空对象（行为不变），计数后聚合 warn 一次——热路径逐条刷屏。
        let mut corrupted: usize = 0;
        let mut first_corrupt: Option<(i64, String, String)> = None; // (seq, message_id, 原因)
        let msgs = stmt
            .query_map(param_refs.as_slice(), |row| {
                let seq: i64 = row.get(0)?;
                let message_id: Option<String> = row.get(1)?;
                let blob_id: Option<String> = row.get(2)?;
                let blob_data: Option<Vec<u8>> = row.get(6)?;
                let (msg, reason) = decode_slot_message(blob_id.as_deref(), blob_data.as_deref());
                if let Some(reason) = reason {
                    corrupted += 1;
                    if first_corrupt.is_none() {
                        first_corrupt = Some((seq, message_id.clone().unwrap_or_default(), reason));
                    }
                }
                Ok(slot_row_to_record(
                    &msg,
                    seq,
                    message_id.unwrap_or_default(),
                    blob_id,
                    row.get(3)?,
                    row.get(4)?,
                    row.get(5)?,
                ))
            })?
            .collect::<Result<Vec<_>, _>>()?;
        if corrupted > 0 {
            if let Some((seq, message_id, reason)) = first_corrupt {
                warn!(
                    pipeline_id = %pipeline_id,
                    tenant_id = %tenant_id,
                    count = corrupted,
                    first_seq = seq,
                    message_id = %message_id,
                    reason = %reason,
                    "message_slots 的消息 blob 损坏，受影响消息降级为空对象"
                );
            }
        }
        // 尾锚定窗口取回后反转为 ASC（对外顺序契约不变）。
        let mut msgs = msgs;
        if tail_anchored {
            msgs.reverse();
        }
        Ok(msgs)
    }

    /// 冷启动历史读路径：join blobs 重建**完整消息对象数组**（含 seq），零回放。
    ///
    /// server 冷路径直读本表拿到 `state["messages"]` 工作集——消息队列的持久真值
    /// 就是 message_slots（实时投影），不依赖 checkpoint/traces 回放。
    /// 返回元素 = 消息 JSON 原样（含 tool_calls/tool_result envelope 等全部字段）+ `seq`。
    pub fn load_message_history(
        &self,
        pipeline_id: &str,
        tenant_id: &str,
    ) -> Result<Vec<serde_json::Value>, StorageError> {
        let conn = self.conn.lock();
        let mut stmt = conn.prepare(
            "SELECT s.seq, s.message_id, s.blob_id, b.data FROM message_slots s \
             LEFT JOIN blobs b ON s.blob_id = b.blob_id \
             WHERE s.pipeline_id = ?1 AND s.tenant_id = ?2 \
             ORDER BY s.seq ASC",
        )?;
        // 与 get_slot_messages_by_pipeline 同款：合法缺失与损坏分开，损坏聚合 warn。
        let mut corrupted: usize = 0;
        let mut first_corrupt: Option<(i64, String, String)> = None;
        let rows = stmt
            .query_map(rusqlite::params![pipeline_id, tenant_id], |row| {
                let seq: i64 = row.get(0)?;
                let message_id: Option<String> = row.get(1)?;
                let blob_id: Option<String> = row.get(2)?;
                let blob_data: Option<Vec<u8>> = row.get(3)?;
                let (mut msg, reason) =
                    decode_slot_message(blob_id.as_deref(), blob_data.as_deref());
                if let Some(reason) = reason {
                    corrupted += 1;
                    if first_corrupt.is_none() {
                        first_corrupt = Some((seq, message_id.unwrap_or_default(), reason));
                    }
                }
                // 槽位号塞回消息对象（内存稠密数组的元素形态：自带稳定 seq）
                if let Some(o) = msg.as_object_mut() {
                    o.insert("seq".to_string(), serde_json::json!(seq));
                }
                Ok(msg)
            })?
            .collect::<Result<Vec<_>, _>>()?;
        if corrupted > 0 {
            if let Some((seq, message_id, reason)) = first_corrupt {
                warn!(
                    pipeline_id = %pipeline_id,
                    tenant_id = %tenant_id,
                    count = corrupted,
                    first_seq = seq,
                    message_id = %message_id,
                    reason = %reason,
                    "message_slots 的消息 blob 损坏，受影响消息降级为空对象"
                );
            }
        }
        Ok(rows)
    }

    /// upsert 一个 state 标量字段到 pipeline_state 表（覆盖最新值，O(1)）。
    ///
    /// 累计语义：投影层无脑覆盖；累加智能在插件里（它读 state 旧值 + 本轮）。
    /// 重建时从 pipeline_state 读出累计值喂回 state，插件自然累加，不归零。
    pub fn upsert_state_field(
        &self,
        pipeline_id: &str,
        tenant_id: &str,
        key: &str,
        value: &serde_json::Value,
    ) -> Result<(), StorageError> {
        let conn = self.conn.lock();
        let value_json =
            serde_json::to_string(value).expect("serde_json Value serialization is infallible");
        let now = chrono::Utc::now().to_rfc3339();
        conn.execute(
            "INSERT INTO pipeline_state (pipeline_id, field_key, field_value, tenant_id, updated_at) \
             VALUES (?1,?2,?3,?4,?5) \
             ON CONFLICT(pipeline_id, field_key, tenant_id) DO UPDATE SET field_value=?3, updated_at=?5",
            rusqlite::params![pipeline_id, key, value_json, tenant_id, now],
        )?;
        Ok(())
    }

    /// upsert 一批 state 标量字段到 pipeline_state 表（B6 批量事务版）。
    ///
    /// 单事务包 N 个 upsert：任一失败整体回滚（全成才 commit）——消灭
    /// 「逐键独立 autocommit，首键失败 = DB 半套，重启后旧值复活」的
    /// 部分写入面。覆盖语义与单键版一致（无脑覆盖最新值）。
    pub fn upsert_state_fields(
        &self,
        pipeline_id: &str,
        tenant_id: &str,
        fields: &serde_json::Map<String, serde_json::Value>,
    ) -> Result<(), StorageError> {
        let mut conn = self.conn.lock();
        let tx = conn
            .transaction()
            .map_err(|e| StorageError::Database(format!("begin tx: {e}")))?;
        match Self::upsert_state_fields_tx(&tx, pipeline_id, tenant_id, fields) {
            Ok(()) => tx
                .commit()
                .map(|_| ())
                .map_err(|e| StorageError::Database(format!("commit: {e}"))),
            Err(e) => Err(e),
        }
    }

    /// [`Self::upsert_state_fields`] 的事务体（tx 由调用方开启并提交/回滚）。
    fn upsert_state_fields_tx(
        tx: &rusqlite::Transaction<'_>,
        pipeline_id: &str,
        tenant_id: &str,
        fields: &serde_json::Map<String, serde_json::Value>,
    ) -> Result<(), StorageError> {
        let now = chrono::Utc::now().to_rfc3339();
        for (key, value) in fields {
            let value_json =
                serde_json::to_string(value).expect("serde_json Value serialization is infallible");
            tx.execute(
                "INSERT INTO pipeline_state (pipeline_id, field_key, field_value, tenant_id, updated_at)                  VALUES (?1,?2,?3,?4,?5)                  ON CONFLICT(pipeline_id, field_key, tenant_id) DO UPDATE SET field_value=?3, updated_at=?5",
                rusqlite::params![pipeline_id, key, value_json, tenant_id, now],
            )?;
        }
        Ok(())
    }

    /// 读出某 pipeline 的全部持久化标量字段（冷启动重建用）。
    pub fn load_pipeline_state(
        &self,
        pipeline_id: &str,
        tenant_id: &str,
    ) -> Result<std::collections::HashMap<String, serde_json::Value>, StorageError> {
        let conn = self.conn.lock();
        let rows = conn
            .prepare("SELECT field_key, field_value FROM pipeline_state WHERE pipeline_id=?1 AND tenant_id=?2")?
            .query_map(rusqlite::params![pipeline_id, tenant_id], |row| {
                Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
            })?
            .collect::<Result<Vec<_>, _>>()?;
        let mut map = std::collections::HashMap::new();
        for (k, v) in rows {
            // 字段 JSON 腐败显式留痕后丢弃——静默丢字段会让冷启动重建悄悄缺值。
            match serde_json::from_str::<serde_json::Value>(&v) {
                Ok(val) => {
                    map.insert(k, val);
                }
                Err(e) => warn!(
                    pipeline_id = %pipeline_id,
                    tenant_id = %tenant_id,
                    field = %k,
                    error = %e,
                    "pipeline_state 字段 JSON 腐败，重建时跳过该字段"
                ),
            }
        }
        Ok(map)
    }

    // ── 域11：pending 输入队列（ADR-2026-08-26）──────────────────────

    /// 入队一条 pending 输入（created_at 即 FIFO 序）。幂等：同 id 重复入队忽略
    /// （INSERT OR IGNORE——重复派发事件不产生重复条目）。
    pub fn enqueue_pending_input(
        &self,
        tenant_id: &str,
        pipeline_id: &str,
        input: &PendingInputRecord,
    ) -> Result<(), StorageError> {
        let conn = self.conn.lock();
        let ec = input
            .execution_context
            .as_ref()
            .map(serde_json::to_string)
            .transpose()
            .map_err(|e| StorageError::Database(e.to_string()))?;
        let ov = input
            .state_overlay
            .as_ref()
            .map(serde_json::to_string)
            .transpose()
            .map_err(|e| StorageError::Database(e.to_string()))?;
        conn.execute(
            "INSERT OR IGNORE INTO pipeline_pending_inputs \
             (id, pipeline_id, tenant_id, user_id, content, thread, source, agent_id, \
              route_id, thinking_strength, client_message_id, execution_context, \
              state_overlay, created_at) \
             VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11,?12,?13,?14)",
            rusqlite::params![
                input.id,
                pipeline_id,
                tenant_id,
                input.user_id,
                input.content,
                input.thread,
                serde_json::to_string(&input.source)
                    .expect("serde_json Value serialization is infallible"),
                input.agent_id,
                input.route_id,
                input.thinking_strength,
                input.client_message_id,
                ec,
                ov,
                input.created_at,
            ],
        )?;
        Ok(())
    }

    /// pending 行 source 列解析。腐败值显式留痕后保守回退 user 标注——
    /// 不留痕会把"读不懂"伪装成真实来源，操作面无从发现。
    fn parse_pending_source(raw: &str, row_id: &str) -> PendingInputSource {
        serde_json::from_str(raw).unwrap_or_else(|e| {
            warn!(
                id = %row_id,
                raw = %raw,
                error = %e,
                "pending 输入 source 腐败，回退 user 标注"
            );
            PendingInputSource::User
        })
    }

    /// pending 行可选 JSON 列（execution_context / state_overlay）解析。
    /// 腐败值显式留痕后丢弃该字段（None）——载荷注入失败必须可观测。
    fn parse_pending_optional_json(
        raw: Option<&str>,
        row_id: &str,
        field: &str,
    ) -> Option<serde_json::Value> {
        let s = raw?;
        match serde_json::from_str(s) {
            Ok(v) => Some(v),
            Err(e) => {
                warn!(id = %row_id, field = field, error = %e, "pending 输入 JSON 字段腐败，丢弃");
                None
            }
        }
    }

    /// 取队首（FIFO：created_at, id 升序）第一条 pending 输入并删除（消费瞬态）。
    /// None = 队列空。删除即激活——激活后不可回退（物理边界）。
    pub fn pop_pending_input(
        &self,
        tenant_id: &str,
        pipeline_id: &str,
    ) -> Result<Option<PendingInputRecord>, StorageError> {
        let conn = self.conn.lock();
        let row = conn
            .query_row(
                "SELECT id, pipeline_id, tenant_id, user_id, content, thread, source, \
                        agent_id, route_id, thinking_strength, client_message_id, \
                        execution_context, state_overlay, created_at \
                 FROM pipeline_pending_inputs \
                 WHERE tenant_id=?1 AND pipeline_id=?2 \
                 ORDER BY created_at, id LIMIT 1",
                rusqlite::params![tenant_id, pipeline_id],
                |row| {
                    Ok((
                        row.get::<_, String>(0)?,
                        row.get::<_, String>(1)?,
                        row.get::<_, String>(2)?,
                        row.get::<_, String>(3)?,
                        row.get::<_, String>(4)?,
                        row.get::<_, String>(5)?,
                        row.get::<_, String>(6)?,
                        row.get::<_, String>(7)?,
                        row.get::<_, String>(8)?,
                        row.get::<_, String>(9)?,
                        row.get::<_, String>(10)?,
                        row.get::<_, Option<String>>(11)?,
                        row.get::<_, Option<String>>(12)?,
                        row.get::<_, String>(13)?,
                    ))
                },
            )
            .optional()?;
        let Some((
            id,
            pid,
            tnt,
            uid,
            content,
            thread,
            source,
            agent,
            route_id,
            thinking,
            cmid,
            ec,
            ov,
            created,
        )) = row
        else {
            return Ok(None);
        };
        conn.execute(
            "DELETE FROM pipeline_pending_inputs WHERE id=?1 AND tenant_id=?2",
            rusqlite::params![id, tenant_id],
        )?;
        let source = Self::parse_pending_source(&source, &id);
        let execution_context =
            Self::parse_pending_optional_json(ec.as_deref(), &id, "execution_context");
        let state_overlay = Self::parse_pending_optional_json(ov.as_deref(), &id, "state_overlay");
        Ok(Some(PendingInputRecord {
            id,
            pipeline_id: pid,
            tenant_id: tnt,
            user_id: uid,
            content,
            thread,
            source,
            agent_id: agent,
            route_id,
            thinking_strength: thinking,
            client_message_id: cmid,
            execution_context,
            state_overlay,
            created_at: created,
        }))
    }

    /// 列出某管道全部 pending 条目（FIFO 序）。
    pub fn list_pending_inputs(
        &self,
        tenant_id: &str,
        pipeline_id: &str,
    ) -> Result<Vec<PendingInputRecord>, StorageError> {
        let conn = self.conn.lock();
        let rows = conn
            .prepare(
                "SELECT id, pipeline_id, tenant_id, user_id, content, thread, source, \
                        agent_id, route_id, thinking_strength, client_message_id, \
                        execution_context, state_overlay, created_at \
                 FROM pipeline_pending_inputs \
                 WHERE tenant_id=?1 AND pipeline_id=?2 \
                 ORDER BY created_at, id",
            )?
            .query_map(rusqlite::params![tenant_id, pipeline_id], |row| {
                Ok((
                    row.get::<_, String>(0)?,
                    row.get::<_, String>(1)?,
                    row.get::<_, String>(2)?,
                    row.get::<_, String>(3)?,
                    row.get::<_, String>(4)?,
                    row.get::<_, String>(5)?,
                    row.get::<_, String>(6)?,
                    row.get::<_, String>(7)?,
                    row.get::<_, String>(8)?,
                    row.get::<_, String>(9)?,
                    row.get::<_, String>(10)?,
                    row.get::<_, Option<String>>(11)?,
                    row.get::<_, Option<String>>(12)?,
                    row.get::<_, String>(13)?,
                ))
            })?
            .collect::<Result<Vec<_>, _>>()?;
        Ok(rows
            .into_iter()
            .map(
                |(
                    id,
                    rid,
                    t,
                    uid,
                    content,
                    thread,
                    source,
                    agent,
                    route_id,
                    thinking,
                    cmid,
                    ec,
                    ov,
                    created,
                )| {
                    let source = Self::parse_pending_source(&source, &id);
                    let execution_context =
                        Self::parse_pending_optional_json(ec.as_deref(), &id, "execution_context");
                    let state_overlay =
                        Self::parse_pending_optional_json(ov.as_deref(), &id, "state_overlay");
                    PendingInputRecord {
                        id,
                        pipeline_id: rid,
                        tenant_id: t,
                        user_id: uid,
                        content,
                        thread,
                        source,
                        agent_id: agent,
                        route_id,
                        thinking_strength: thinking,
                        client_message_id: cmid,
                        execution_context,
                        state_overlay,
                        created_at: created,
                    }
                },
            )
            .collect())
    }

    /// 修改 pending 条目 content（不存在 → Ok(false)）。
    pub fn update_pending_input_content(
        &self,
        tenant_id: &str,
        pipeline_id: &str,
        input_id: &str,
        new_content: &str,
    ) -> Result<bool, StorageError> {
        let conn = self.conn.lock();
        let n = conn.execute(
            "UPDATE pipeline_pending_inputs SET content=?1 \
             WHERE id=?2 AND tenant_id=?3 AND pipeline_id=?4",
            rusqlite::params![new_content, input_id, tenant_id, pipeline_id],
        )?;
        Ok(n > 0)
    }

    /// 删除单条 pending 输入（不存在 → Ok(false)）。
    pub fn delete_pending_input(
        &self,
        tenant_id: &str,
        pipeline_id: &str,
        input_id: &str,
    ) -> Result<bool, StorageError> {
        let conn = self.conn.lock();
        let n = conn.execute(
            "DELETE FROM pipeline_pending_inputs \
             WHERE id=?1 AND tenant_id=?2 AND pipeline_id=?3",
            rusqlite::params![input_id, tenant_id, pipeline_id],
        )?;
        Ok(n > 0)
    }

    /// 清空管道全部 pending 输入，返回删除条数。
    pub fn clear_pending_inputs(
        &self,
        tenant_id: &str,
        pipeline_id: &str,
    ) -> Result<usize, StorageError> {
        let conn = self.conn.lock();
        let n = conn.execute(
            "DELETE FROM pipeline_pending_inputs WHERE tenant_id=?1 AND pipeline_id=?2",
            rusqlite::params![tenant_id, pipeline_id],
        )?;
        Ok(n)
    }

    /// 保存**标量基线** checkpoint（每 N 步 / run 结束调用，留档用）。
    ///
    /// 瘦身（任务 5）：messages **不进 checkpoint**——消息队列持久真值在
    /// message_slots 表（实时投影），checkpoint 只留标量字段 + `ckpt_max_seq`
    /// 水位（当时队列的最大槽位号），冷启动 messages 直读表、零回放。
    /// 全文冗余消除：messages 全文不进 checkpoint，避免整份 state 逐 run 重复抄写。
    ///
    /// per-run 易变键剥离（GAP-3）：剥离集 = 内核自有键（VOLATILE_RUN_KEYS）
    /// ∪ 插件声明键（P1-4 声明化，经 `set_declared_volatile_keys` 注入）——
    /// 剥离在落档点单点消费，任何经本 store 的 checkpoint 写入（含直连
    /// trait 的嵌入方）都不得携带 per-run 键。
    pub fn save_checkpoint(
        &self,
        pipeline_id: &str,
        tenant_id: &str,
        step_no: i64,
        state: &serde_json::Value,
    ) -> Result<(), StorageError> {
        let conn = self.conn.lock();
        let checkpoint_id = format!("cp_{}_{}", pipeline_id, step_no);
        // 瘦身副本：剥离 messages + 易变 per-run 键（内核自有 ∪ 插件声明），
        // 写 ckpt_max_seq 水位（原 state 不动）。易变键（GAP-3）：
        // message/input/message_id/suspended 等属于"本轮运行"，不是管道累计
        // 状态——若残留，冷恢复时会覆盖下一轮的新输入，重启后旧 user 消息
        // 被重放消费。
        let mut slim = state.clone();
        if let Some(obj) = slim.as_object_mut() {
            let max_seq = obj
                .get("messages")
                .and_then(|v| v.as_array())
                .map(|a| {
                    a.iter()
                        .filter_map(|m| m.get("seq").and_then(|s| s.as_i64()))
                        .max()
                        .unwrap_or(-1)
                })
                .unwrap_or(-1);
            obj.remove("messages");
            for k in VOLATILE_RUN_KEYS {
                obj.remove(*k);
            }
            for k in self.declared_volatile_keys.read().iter() {
                obj.remove(k.as_str());
            }
            obj.insert("ckpt_max_seq".to_string(), serde_json::json!(max_seq));
        }
        let state_json =
            serde_json::to_string(&slim).expect("serde_json Value serialization is infallible");
        let now = chrono::Utc::now().to_rfc3339();
        // INSERT OR REPLACE：同一 step 重放幂等
        conn.execute(
            "INSERT OR REPLACE INTO pipeline_checkpoints \
             (checkpoint_id, pipeline_id, step_no, state_json, tenant_id, created_at) \
             VALUES (?1,?2,?3,?4,?5,?6)",
            rusqlite::params![
                checkpoint_id,
                pipeline_id,
                step_no,
                state_json,
                tenant_id,
                now
            ],
        )?;
        Ok(())
    }

    /// 取最近一个 checkpoint（step_no 最大），返回 (step_no, state_json)。
    pub fn load_latest_checkpoint(
        &self,
        pipeline_id: &str,
        tenant_id: &str,
    ) -> Result<Option<(i64, serde_json::Value)>, StorageError> {
        let conn = self.conn.lock();
        let row = conn
            .query_row(
                "SELECT step_no, state_json FROM pipeline_checkpoints \
                 WHERE pipeline_id=?1 AND tenant_id=?2 ORDER BY step_no DESC LIMIT 1",
                rusqlite::params![pipeline_id, tenant_id],
                |row| Ok((row.get::<_, i64>(0)?, row.get::<_, String>(1)?)),
            )
            .optional()?;
        match row {
            Some((step_no, state_json)) => {
                let mut state: serde_json::Value =
                    serde_json::from_str(&state_json).map_err(|e| {
                        StorageError::Serialization(format!(
                            "checkpoint state for pipeline {pipeline_id}: {e}"
                        ))
                    })?;
                // 零兼容：messages 一律剥离（队列真值在 message_slots，旧全量 checkpoint 亦不消费）
                if let Some(obj) = state.as_object_mut() {
                    obj.remove("messages");
                }
                Ok(Some((step_no, state)))
            }
            None => Ok(None),
        }
    }

    /// 枚举租户内带持久化 state 的管道 (pipeline_id, thread_id)（冷读 DB 兜底）。
    /// 来源 = pipeline_state 标量 ∪ checkpoint（二者覆盖任务完成态与执行快照）；
    /// thread_id 以 pipeline_sessions 为准，缺省回退 pipeline_id（任务管道自持）。
    pub fn list_state_pipeline_ids(
        &self,
        tenant_id: &str,
    ) -> Result<Vec<(String, String)>, StorageError> {
        let conn = self.conn.lock();
        let mut stmt = conn.prepare(
            "SELECT t.pipeline_id, COALESCE(ses.thread_id, t.pipeline_id) AS thread_id \
             FROM ( \
                 SELECT pipeline_id FROM pipeline_state WHERE tenant_id = ?1 \
                 UNION \
                 SELECT pipeline_id FROM pipeline_checkpoints WHERE tenant_id = ?1 \
             ) t \
             LEFT JOIN pipeline_sessions ses \
                    ON ses.pipeline_id = t.pipeline_id AND ses.tenant_id = ?1 \
             ORDER BY t.pipeline_id",
        )?;
        let rows = stmt.query_map(rusqlite::params![tenant_id], |row| {
            Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
        })?;
        let mut out = Vec::new();
        for row in rows {
            out.push(row?);
        }
        Ok(out)
    }

    /// 在已有锁定连接上确保 blob 存在，返回 (blob_id, mime)。
    /// project_messages 复用同锁内的 conn，避免重复加锁死锁。
    fn ensure_blob_locked(
        &self,
        conn: &Connection,
        content: &str,
    ) -> Result<(Option<String>, &'static str), StorageError> {
        if content.is_empty() {
            return Ok((None, "text/plain"));
        }
        let blob_id = agentos_core::ids::compute_blob_id(content.as_bytes());
        let exists: bool = conn
            .query_row(
                "SELECT COUNT(*) > 0 FROM blobs WHERE blob_id = ?1",
                rusqlite::params![blob_id],
                |row| row.get(0),
            )
            .unwrap_or(false);
        if !exists {
            let now = chrono::Utc::now().to_rfc3339();
            conn.execute(
                "INSERT INTO blobs (blob_id, mime_type, size_bytes, data, created_at) VALUES (?1,?2,?3,?4,?5)",
                rusqlite::params![blob_id, "text/plain", content.len() as i64, content.as_bytes(), now],
            )?;
        }
        Ok((Some(blob_id), "text/plain"))
    }

    // ── 域2：session 标签夹内部方法 ──────────────
    // tenant_id 从 task_local 取。pipeline_ids / metadata 以 JSON 文本存储。

    /// upsert 会话（存在则更新，不存在则插入）。
    /// tenant_id 由调用方在派发到 DB 线程池前解析（task_local 不跨池线程）。
    fn upsert_session_inner(
        &self,
        session: &SessionRecord,
        tenant_id: &str,
    ) -> Result<(), StorageError> {
        let pipeline_ids_json = serde_json::to_string(&session.pipeline_ids)
            .map_err(|e| StorageError::Database(format!("serialize pipeline_ids: {e}")))?;
        let metadata_json = session.metadata.as_ref().map(|v| {
            serde_json::to_string(v).expect("serde_json Value serialization is infallible")
        });
        let conn = self.conn.lock();
        conn.execute(
            "INSERT INTO sessions (thread_id, title, intent, current_state, agent_id, active_pipeline_id, pipeline_ids, metadata, tenant_id, created_at, updated_at, last_active_at)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12)
             ON CONFLICT(thread_id) DO UPDATE SET
                title = excluded.title,
                intent = excluded.intent,
                current_state = excluded.current_state,
                agent_id = excluded.agent_id,
                active_pipeline_id = excluded.active_pipeline_id,
                pipeline_ids = excluded.pipeline_ids,
                metadata = excluded.metadata,
                updated_at = excluded.updated_at,
                last_active_at = excluded.last_active_at",
            rusqlite::params![
                session.thread_id,
                session.title,
                session.intent,
                session.current_state,
                session.agent_id,
                session.active_pipeline_id,
                pipeline_ids_json,
                metadata_json,
                tenant_id,
                session.created_at,
                session.updated_at,
                session.last_active_at,
            ],
        )?;
        Ok(())
    }

    /// 级联删除会话及其全部关联数据（主管道 + 子任务管道的执行数据，含 state）。
    ///
    /// 通过 `pipeline_sessions` 映射表按 thread_id 找到该会话下所有 pipeline_id
    /// （主管道 + 子管道，无需父子关系；sessions.pipeline_ids JSON 兜底防主管道
    /// 未写映射），再沿血缘 lineage.parent_pipeline_id 补全后代任务管道（子任务
    /// 自环绑定不属于任何会话），整组交给 [`Self::delete_pipelines_cascade`]
    /// 清理，最后删 sessions 行。返回实际删除的 pipeline_id 清单（调用方逐出
    /// 内存 state 注册表用）。无记录时同样返回 Ok(vec![])（幂等）。
    /// tenant_id 由调用方在派发到 DB 线程池前解析。单次事务包裹，失败回滚。
    fn delete_session_inner(
        &self,
        thread_id: &str,
        tenant_id: &str,
    ) -> Result<Vec<String>, StorageError> {
        let mut conn = self.conn.lock();
        let tx = conn
            .transaction()
            .map_err(|e| StorageError::Database(format!("begin tx: {e}")))?;

        // 1. 收集该会话全部 pipeline_id：映射表 + sessions.pipeline_ids 兜底（防主管道未写映射）。
        let mut pipeline_ids = pipeline_ids_for_thread(&tx, thread_id, tenant_id)?;

        // 2. 血缘后代：子任务管道出生落自环绑定（thread=自身 id），不属于任何
        // 会话——按 thread 清单级联会漏掉整条子任务链（runs/state/绑定全残留，
        // 管理页幽灵条目的根源），删除面清单必须沿血缘补全。
        let descendants = descendant_pipeline_ids(&tx, &pipeline_ids, tenant_id)?;
        pipeline_ids.extend(descendants);

        if pipeline_ids.is_empty() {
            // 无任何管道（纯标签会话）：直接删 sessions 行
            tx.execute(
                "DELETE FROM sessions WHERE thread_id = ?1 AND tenant_id = ?2",
                rusqlite::params![thread_id, tenant_id],
            )?;
            return tx
                .commit()
                .map(|_| Vec::new())
                .map_err(|e| StorageError::Database(format!("commit: {e}")));
        }

        // 3. 级联清理全部关联管道的执行数据，清单见 delete_pipelines_cascade
        Self::delete_pipelines_cascade(&tx, &pipeline_ids, tenant_id)?;

        // 4. 删 sessions 行
        tx.execute(
            "DELETE FROM sessions WHERE thread_id = ?1 AND tenant_id = ?2",
            rusqlite::params![thread_id, tenant_id],
        )?;

        tx.commit()
            .map(|_| pipeline_ids)
            .map_err(|e| StorageError::Database(format!("commit: {e}")))
    }

    /// 按 pipeline_id 删除单条管道的全部执行数据（任务删除语义）。
    ///
    /// 0.2 任务 = 管道（GAP-1）：删除任务即删除其管道数据。级联清单与
    /// `delete_session_inner` 共用 [`Self::delete_pipelines_cascade`]，
    /// 两份清单不得各自增删表。清单先沿血缘补全后代任务管道（删父任务
    /// 连带其派生链，否则子任务自环绑定让整链残留成幽灵）。
    /// 单次事务包裹，失败回滚；无记录时返回 Ok(vec![])（幂等）。
    fn delete_pipeline_inner(
        &self,
        pipeline_id: &str,
        tenant_id: &str,
    ) -> Result<Vec<String>, StorageError> {
        let mut conn = self.conn.lock();
        let tx = conn
            .transaction()
            .map_err(|e| StorageError::Database(format!("begin tx: {e}")))?;

        let mut pipeline_ids = vec![pipeline_id.to_string()];
        let descendants = descendant_pipeline_ids(&tx, &pipeline_ids, tenant_id)?;
        pipeline_ids.extend(descendants);

        Self::delete_pipelines_cascade(&tx, &pipeline_ids, tenant_id)?;

        tx.commit()
            .map(|_| pipeline_ids)
            .map_err(|e| StorageError::Database(format!("commit: {e}")))
    }

    /// 会话删除 / 任务删除共用的管道级联清理：删净一组管道在全部执行数据表中的行。
    ///
    /// run 域（traces/branches/runs）按 message_slots 反查的 run_id 删；
    /// 管道域（message_slots/pipeline_state/pipeline_checkpoints/
    /// pipeline_pending_inputs/pipeline_sessions）按 pipeline_id 删。
    /// state 两表与 pending 队列不清会留幽灵任务：任务列表从 pipeline_state
    /// 的 `task.*` 键派生；pending 队列重启后会被消费续跑，令已删任务复活。
    /// `blobs` 是内容寻址去重存储、无管道外键，不入级联（仅 clear-all 清）。
    /// 事务由调用方开启；`pipeline_ids` 须非空（空 IN 列表非法）。
    fn delete_pipelines_cascade(
        tx: &rusqlite::Transaction<'_>,
        pipeline_ids: &[String],
        tenant_id: &str,
    ) -> Result<(), StorageError> {
        // run 域数据按 run_id 删
        let run_ids: Vec<String> = run_ids_of_pipelines(tx, pipeline_ids, tenant_id)?;
        if !run_ids.is_empty() {
            for sql in [
                "DELETE FROM traces WHERE run_id IN ({placeholders})",
                "DELETE FROM branches WHERE run_id IN ({placeholders})",
                "DELETE FROM runs WHERE run_id IN ({placeholders})",
            ] {
                Self::delete_in_clause(tx, sql, &run_ids, "")?;
            }
        }
        // 管道域数据按 pipeline_id 删
        for sql in [
            "DELETE FROM message_slots WHERE pipeline_id IN ({placeholders}) AND tenant_id = ?",
            "DELETE FROM pipeline_state WHERE pipeline_id IN ({placeholders}) AND tenant_id = ?",
            "DELETE FROM pipeline_checkpoints WHERE pipeline_id IN ({placeholders}) AND tenant_id = ?",
            "DELETE FROM pipeline_pending_inputs WHERE pipeline_id IN ({placeholders}) AND tenant_id = ?",
            "DELETE FROM pipeline_sessions WHERE pipeline_id IN ({placeholders}) AND tenant_id = ?",
        ] {
            Self::delete_in_clause(tx, sql, pipeline_ids, tenant_id)?;
        }
        Ok(())
    }

    /// 辅助：按 IN (?, ?, ...) 占位符执行 DELETE。
    /// `sql_template` 中用 `{placeholders}` 标记占位符位置；`extra` 为可选的额外参数（如 tenant_id）。
    fn delete_in_clause(
        tx: &rusqlite::Transaction<'_>,
        sql_template: &str,
        values: &[String],
        extra: &str,
    ) -> Result<(), StorageError> {
        let placeholders = (0..values.len())
            .map(|i| format!("?{}", i + 1))
            .collect::<Vec<_>>()
            .join(", ");
        let sql = sql_template.replace("{placeholders}", &placeholders);
        let mut params: Vec<&dyn rusqlite::ToSql> =
            values.iter().map(|p| p as &dyn rusqlite::ToSql).collect();
        if !extra.is_empty() {
            params.push(&extra);
        }
        tx.execute(&sql, params.as_slice())?;
        Ok(())
    }

    /// 写入 pipeline↔session 映射（幂等：INSERT OR IGNORE）。
    /// 在 persist_run_start 时调用，确保每个管道（主管道/子管道）都记录所属会话。
    /// tenant_id 由调用方在派发到 DB 线程池前解析。
    fn link_pipeline_session_inner(
        &self,
        pipeline_id: &str,
        thread_id: &str,
        tenant_id: &str,
    ) -> Result<(), StorageError> {
        // 两个 id 均为内部生成的 uuid（persist_run_start / 会话创建路径）；
        // 为空即上游 bug，报错可见，不得静默跳过（跳过会丢管道-会话映射）。
        if pipeline_id.is_empty() || thread_id.is_empty() {
            return Err(StorageError::Database(format!(
                "link_pipeline_session: empty id (pipeline_id={pipeline_id:?}, thread_id={thread_id:?})"
            )));
        }
        let conn = self.conn.lock();
        let now = chrono::Utc::now().to_rfc3339();
        conn.execute(
            "INSERT OR IGNORE INTO pipeline_sessions (pipeline_id, thread_id, tenant_id, created_at) VALUES (?1, ?2, ?3, ?4)",
            rusqlite::params![pipeline_id, thread_id, tenant_id, now],
        )?;
        Ok(())
    }

    /// 查询某会话下的全部 pipeline_id（主管道 + 子管道）。
    /// tenant_id 由调用方在派发到 DB 线程池前解析。
    fn list_pipeline_ids_by_thread_inner(
        &self,
        thread_id: &str,
        tenant_id: &str,
    ) -> Result<Vec<String>, StorageError> {
        let conn = self.conn.lock();
        let mut stmt = conn.prepare(
            "SELECT pipeline_id FROM pipeline_sessions WHERE thread_id = ?1 AND tenant_id = ?2",
        )?;
        let rows = stmt.query_map(rusqlite::params![thread_id, tenant_id], |row| {
            row.get::<_, String>(0)
        })?;
        let mut out = Vec::new();
        for r in rows {
            out.push(r?);
        }
        Ok(out)
    }

    /// 按管道唯一坐标反查所属会话 thread_id（注入分支坐标解析）。
    /// 只查 pipeline_sessions（唯一真值源），未命中即 None，不做 sessions 回退。
    /// pipeline_id 全局唯一（uuid），单查无歧义。
    fn get_thread_id_by_pipeline_inner(
        &self,
        pipeline_id: &str,
    ) -> Result<Option<String>, StorageError> {
        let conn = self.conn.lock();
        let row = conn.query_row(
            "SELECT thread_id FROM pipeline_sessions WHERE pipeline_id = ?1",
            rusqlite::params![pipeline_id],
            |row| row.get::<_, String>(0),
        );
        match row {
            Ok(t) => Ok(Some(t)),
            Err(rusqlite::Error::QueryReturnedNoRows) => Ok(None),
            Err(e) => Err(e.into()),
        }
    }

    /// 按 thread_id 取单个会话。tenant_id 由调用方在派发到 DB 线程池前解析。
    fn get_session_inner(
        &self,
        thread_id: &str,
        tenant_id: &str,
    ) -> Result<Option<SessionRecord>, StorageError> {
        let conn = self.conn.lock();
        let row = conn.query_row(
            "SELECT thread_id, title, intent, current_state, agent_id, active_pipeline_id, pipeline_ids, metadata, created_at, updated_at, last_active_at
             FROM sessions WHERE thread_id = ?1 AND tenant_id = ?2",
            rusqlite::params![thread_id, tenant_id],
            Self::row_to_session,
        );
        match row {
            Ok(s) => Ok(Some(s)),
            Err(rusqlite::Error::QueryReturnedNoRows) => Ok(None),
            Err(e) => Err(e.into()),
        }
    }

    /// 列会话（可选按 session_type 过滤，按 updated_at 倒序）。
    /// tenant_id 由调用方在派发到 DB 线程池前解析（task_local 不跨池线程）。
    fn list_sessions_inner(
        &self,
        filter: &SessionListFilter,
        tenant_id: &str,
    ) -> Result<Vec<SessionRecord>, StorageError> {
        let conn = self.conn.lock();
        let mut sql = String::from(
            "SELECT thread_id, title, intent, current_state, agent_id, active_pipeline_id, pipeline_ids, metadata, created_at, updated_at, last_active_at
             FROM sessions WHERE tenant_id = ?1",
        );
        // session_type 过滤：JSON metadata 里 session_type 字段匹配
        if filter.session_type.is_some() {
            sql.push_str(" AND json_extract(COALESCE(metadata, '{}'), '$.session_type') = ?2");
        }
        sql.push_str(" ORDER BY updated_at DESC");
        if filter.limit.is_some() {
            sql.push_str(" LIMIT ?3");
        }
        let mut stmt = conn.prepare(&sql)?;
        let mut params: Vec<Box<dyn rusqlite::ToSql>> = vec![Box::new(tenant_id)];
        if let Some(st) = &filter.session_type {
            params.push(Box::new(st.clone()));
        }
        if let Some(lim) = filter.limit {
            params.push(Box::new(lim as i64));
        }
        let param_refs: Vec<&dyn rusqlite::ToSql> = params.iter().map(|p| p.as_ref()).collect();
        let sessions = stmt.query_map(param_refs.as_slice(), Self::row_to_session)?;
        sessions
            .into_iter()
            .collect::<Result<Vec<_>, _>>()
            .map_err(StorageError::from)
    }

    /// 从查询行构造 SessionRecord（pipeline_ids/metadata 反序列化）。
    fn row_to_session(row: &rusqlite::Row<'_>) -> rusqlite::Result<SessionRecord> {
        let pipeline_ids_str: Option<String> = row.get(6)?;
        let metadata_str: Option<String> = row.get(7)?;
        // JSON 列由本 store 写侧 serde_json::to_string 写入（纯内部往返）；
        // 解析失败是数据损坏，报错而非静默清空（清空会让会话丢管道关联）。
        let pipeline_ids: Vec<String> = match pipeline_ids_str.as_deref() {
            None => Vec::new(),
            Some(s) => serde_json::from_str(s).map_err(|e| {
                rusqlite::Error::FromSqlConversionFailure(
                    6,
                    rusqlite::types::Type::Text,
                    Box::new(e),
                )
            })?,
        };
        let metadata = match metadata_str.as_deref() {
            None => None,
            Some(s) => Some(serde_json::from_str(s).map_err(|e| {
                rusqlite::Error::FromSqlConversionFailure(
                    7,
                    rusqlite::types::Type::Text,
                    Box::new(e),
                )
            })?),
        };
        Ok(SessionRecord {
            thread_id: row.get(0)?,
            title: row.get(1)?,
            intent: row.get(2)?,
            current_state: row.get(3)?,
            agent_id: row.get(4)?,
            active_pipeline_id: row.get(5)?,
            pipeline_ids,
            metadata,
            created_at: row.get(8)?,
            updated_at: row.get(9)?,
            last_active_at: row.get(10)?,
        })
    }

    // ── 域6：users（0.5.0 完整用户系统的最小持久化地基）───────────────
    // 一用户一租户：tenant_id = user_id。username 跨租户全局唯一。
    // get_user_by_username 不加 tenant 过滤（登录时还没有租户上下文）。

    /// 创建用户（username 全局唯一约束，重复返回 StorageError）。
    /// user.tenant_id 直接入库（一用户一租户，由调用方设 = user_id）。
    fn create_user_inner(&self, user: &UserRecord) -> Result<(), StorageError> {
        let conn = self.conn.lock();
        conn.execute(
            "INSERT INTO users (user_id, username, password, email, role, tenant_id, created_at, last_login_at, must_change_password)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9)",
            rusqlite::params![
                user.user_id,
                user.username,
                user.password,
                user.email,
                user.role,
                user.tenant_id,
                user.created_at,
                user.last_login_at,
                user.must_change_password,
            ],
        )?;
        Ok(())
    }

    /// 按 user_id 取用户（跨租户全局查询，token 解析用，不加 tenant 过滤）。
    /// user_id 是全局唯一主键，按 user_id 查天然定位唯一用户。
    /// 注意：token 解析场景（resolve_tenant_id_by_user）尚未确定 tenant，
    /// 故不能按 task_local tenant 过滤（否则查不到 → 回退 default → 隔离失效）。
    fn get_user_by_id_inner(&self, user_id: &str) -> Result<Option<UserRecord>, StorageError> {
        let conn = self.conn.lock();
        let row = conn.query_row(
            "SELECT user_id, username, password, email, role, tenant_id, created_at, last_login_at, must_change_password
             FROM users WHERE user_id = ?1",
            rusqlite::params![user_id],
            Self::row_to_user,
        );
        match row {
            Ok(u) => Ok(Some(u)),
            Err(rusqlite::Error::QueryReturnedNoRows) => Ok(None),
            Err(e) => Err(e.into()),
        }
    }

    /// 按用户名取用户（跨租户全局查询，登录用，不加 tenant 过滤）。
    fn get_user_by_username_inner(
        &self,
        username: &str,
    ) -> Result<Option<UserRecord>, StorageError> {
        let conn = self.conn.lock();
        let row = conn.query_row(
            "SELECT user_id, username, password, email, role, tenant_id, created_at, last_login_at, must_change_password
             FROM users WHERE username = ?1",
            rusqlite::params![username],
            Self::row_to_user,
        );
        match row {
            Ok(u) => Ok(Some(u)),
            Err(rusqlite::Error::QueryReturnedNoRows) => Ok(None),
            Err(e) => Err(e.into()),
        }
    }

    /// 列全部用户（跨租户，管理用）。
    fn list_users_inner(&self) -> Result<Vec<UserRecord>, StorageError> {
        let conn = self.conn.lock();
        let mut stmt = conn.prepare(
            "SELECT user_id, username, password, email, role, tenant_id, created_at, last_login_at, must_change_password
                 FROM users ORDER BY created_at ASC",
        )?;
        let users = stmt.query_map([], Self::row_to_user)?;
        users
            .into_iter()
            .collect::<Result<Vec<_>, _>>()
            .map_err(StorageError::from)
    }

    /// 更新最近登录时间。
    fn update_last_login_inner(&self, user_id: &str) -> Result<(), StorageError> {
        let now = chrono::Utc::now().to_rfc3339();
        let conn = self.conn.lock();
        conn.execute(
            "UPDATE users SET last_login_at = ?1 WHERE user_id = ?2",
            rusqlite::params![now, user_id],
        )?;
        Ok(())
    }

    /// 更新口令哈希与首登改密标记。返回是否存在该行。
    fn update_user_password_inner(
        &self,
        user_id: &str,
        password_hash: &str,
        must_change_password: bool,
    ) -> Result<bool, StorageError> {
        let conn = self.conn.lock();
        let affected = conn.execute(
            "UPDATE users SET password = ?1, must_change_password = ?2 WHERE user_id = ?3",
            rusqlite::params![password_hash, must_change_password, user_id],
        )?;
        Ok(affected > 0)
    }

    /// 删除用户。返回是否删除了行。
    fn delete_user_inner(&self, user_id: &str) -> Result<bool, StorageError> {
        let conn = self.conn.lock();
        let affected = conn.execute(
            "DELETE FROM users WHERE user_id = ?1",
            rusqlite::params![user_id],
        )?;
        Ok(affected > 0)
    }

    /// 从查询行构造 UserRecord。
    fn row_to_user(row: &rusqlite::Row<'_>) -> rusqlite::Result<UserRecord> {
        Ok(UserRecord {
            user_id: row.get(0)?,
            username: row.get(1)?,
            password: row.get(2)?,
            email: row.get(3)?,
            role: row.get(4)?,
            tenant_id: row.get(5)?,
            created_at: row.get(6)?,
            last_login_at: row.get(7)?,
            must_change_password: row.get(8)?,
        })
    }

    /// 查询某会话下的 step 级轨迹（冷启动统一回放用）。
    ///
    /// 经 pipeline_sessions 映射表按 thread_id 找到全部 pipeline_id → 这些 pipeline 产生
    /// 的 run_id → 对应 traces。只返回 step 级轨迹（plugin_id 为配置 step id，不以
    /// `pipeline_` 前缀的旧插件级轨迹被忽略），按 created_at 升序以便按序 merge 回放。
    /// tenant_id 由调用方在派发到 DB 线程池前解析。
    fn get_step_traces_by_thread_inner(
        &self,
        thread_id: &str,
        tenant_id: &str,
    ) -> Result<Vec<TraceEntry>, StorageError> {
        // pipeline_id 集合：映射表 + sessions.pipeline_ids 兜底
        let pipeline_ids = pipeline_ids_for_thread(&self.conn.lock(), thread_id, tenant_id)?;
        if pipeline_ids.is_empty() {
            return Ok(vec![]);
        }
        self.get_step_traces_for_pipelines_inner(&pipeline_ids, tenant_id)
    }

    /// 查询单个管道自己的 step 级轨迹（管道执行态冷恢复专用，见 trait 文档）。
    fn get_step_traces_by_pipeline_inner(
        &self,
        pipeline_id: &str,
        tenant_id: &str,
    ) -> Result<Vec<TraceEntry>, StorageError> {
        self.get_step_traces_for_pipelines_inner(&[pipeline_id.to_string()], tenant_id)
    }

    /// 按 pipeline_id 集合取 step 级轨迹（created_at 升序）。
    fn get_step_traces_for_pipelines_inner(
        &self,
        pipeline_ids: &[String],
        tenant_id: &str,
    ) -> Result<Vec<TraceEntry>, StorageError> {
        let conn = self.conn.lock();
        // run_id 集合（经 messages.pipeline_id 反查）
        let run_ids: Vec<String> = run_ids_of_pipelines(&conn, pipeline_ids, tenant_id)?;
        if run_ids.is_empty() {
            return Ok(vec![]);
        }

        // traces：只取 step 级（plugin_id 不以 pipeline_ 开头），按 created_at 升序
        let run_placeholders = (0..run_ids.len())
            .map(|_| "?")
            .collect::<Vec<_>>()
            .join(", ");
        let trace_sql = format!(
            "SELECT trace_id, run_id, branch_id, seq_in_branch, plugin_id, patch_type, patch_data, created_at FROM traces WHERE run_id IN ({run_placeholders}) AND tenant_id = ? AND plugin_id NOT LIKE 'pipeline_%' ORDER BY created_at ASC"
        );
        let mut stmt = conn.prepare(&trace_sql)?;
        let mut params: Vec<&dyn rusqlite::ToSql> =
            run_ids.iter().map(|p| p as &dyn rusqlite::ToSql).collect();
        params.push(&tenant_id);
        let traces = stmt.query_map(params.as_slice(), |row| {
            let plugin_id: String = row.get(4)?;
            let patch_type_str: String = row.get(5)?;
            let patch_data_str: String = row.get(6)?;
            Ok(TraceEntry {
                trace_id: row.get(0)?,
                run_id: row.get(1)?,
                branch_id: row.get(2)?,
                seq_in_branch: row.get(3)?,
                plugin_id: plugin_id.clone(),
                patch_type: parse_patch_type(&patch_type_str, &plugin_id),
                patch_data: serde_json::from_str(&patch_data_str).map_err(|e| {
                    rusqlite::Error::FromSqlConversionFailure(
                        6,
                        rusqlite::types::Type::Text,
                        Box::new(e),
                    )
                })?,
                created_at: row.get(7)?,
            })
        })?;
        traces
            .into_iter()
            .collect::<Result<Vec<_>, _>>()
            .map_err(StorageError::from)
    }

    // ── runs/traces/blobs 热路径同步体 ─────────────────────────
    //
    // 以下六个同步体只该由 [`SqliteStore::blocking`] 在专用 DB 线程池内执行
    // （trait 面 async 包装统一派发）：在 tokio worker 上直接 conn.lock() 会与
    // 池内长事务竞争同一把连接锁，长查询期间可拖住 runtime worker 造成饥饿。
    // task_local 租户只在调用方 task 存在，由 async 包装在派发前读取后随闭包带入。

    fn get_run_inner(&self, run_id: &str, tenant_id: &str) -> Result<RunRecord, StorageError> {
        let conn = self.conn.lock();
        let row = conn.query_row(
            "SELECT run_id, config_hash, status, tenant_id, created_at, ended_at, current_branch, current_seq, metadata FROM runs WHERE run_id = ?1 AND tenant_id = ?2",
            rusqlite::params![run_id, tenant_id],
            |row| {
                let status_str: String = row.get(2)?;
                let metadata_str: Option<String> = row.get(8)?;
                Ok(RunRecord {
                    run_id: row.get(0)?,
                    config_hash: row.get(1)?,
                    status: parse_run_status(&status_str, run_id),
                    tenant_id: row.get(3)?,
                    created_at: row.get(4)?,
                    ended_at: row.get(5)?,
                    current_branch: row.get(6)?,
                    current_seq: row.get::<_, i64>(7)? as u32,
                    metadata: parse_run_metadata(metadata_str, run_id),
                })
            },
        );
        match row {
            Ok(r) => Ok(r),
            Err(rusqlite::Error::QueryReturnedNoRows) => {
                Err(StorageError::NotFound(format!("run not found: {}", run_id)))
            }
            Err(e) => Err(e.into()),
        }
    }

    fn set_run_pipeline_inner(&self, run_id: &str, pipeline_id: &str) -> Result<(), StorageError> {
        let conn = self.conn.lock();
        conn.execute(
            "UPDATE runs SET pipeline_id = ?1 WHERE run_id = ?2",
            rusqlite::params![pipeline_id, run_id],
        )?;
        Ok(())
    }

    fn list_runs_by_pipeline_inner(
        &self,
        pipeline_id: &str,
        tenant_id: &str,
    ) -> Result<Vec<RunRecord>, StorageError> {
        let conn = self.conn.lock();
        let mut stmt = conn.prepare(
            "SELECT run_id, config_hash, status, tenant_id, created_at, ended_at, current_branch, current_seq, metadata FROM runs WHERE pipeline_id = ?1 AND tenant_id = ?2 ORDER BY created_at DESC",
        )?;
        let rows = stmt.query_map(rusqlite::params![pipeline_id, tenant_id], |row| {
            let status_str: String = row.get(2)?;
            let metadata_str: Option<String> = row.get(8)?;
            let run_id: String = row.get(0)?;
            Ok(RunRecord {
                run_id: run_id.clone(),
                config_hash: row.get(1)?,
                status: parse_run_status(&status_str, &run_id),
                tenant_id: row.get(3)?,
                created_at: row.get(4)?,
                ended_at: row.get(5)?,
                current_branch: row.get(6)?,
                current_seq: row.get(7)?,
                metadata: parse_run_metadata(metadata_str, &run_id),
            })
        })?;
        Ok(rows.collect::<Result<Vec<_>, _>>()?)
    }

    fn get_blob_inner(&self, blob_id: &str) -> Result<Vec<u8>, StorageError> {
        let conn = self.conn.lock();
        let row = conn.query_row(
            "SELECT data FROM blobs WHERE blob_id = ?1",
            rusqlite::params![blob_id],
            |row| row.get::<_, Vec<u8>>(0),
        );
        match row {
            Ok(data) => Ok(data),
            Err(rusqlite::Error::QueryReturnedNoRows) => Err(StorageError::NotFound(format!(
                "blob not found: {}",
                blob_id
            ))),
            Err(e) => Err(e.into()),
        }
    }

    fn append_trace_inner(&self, entry: TraceEntry, tenant_id: &str) -> Result<(), StorageError> {
        let conn = self.conn.lock();
        let patch_type_str = match entry.patch_type {
            PatchType::StateUpdate => "state_update",
            PatchType::RouteSignal => "route_signal",
            PatchType::Error => "error",
            PatchType::Lifecycle => "lifecycle",
            PatchType::Rollback => "rollback",
        };
        let patch_data_str = serde_json::to_string(&entry.patch_data)
            .expect("serde_json Value serialization is infallible");
        conn.execute(
            "INSERT INTO traces (trace_id, run_id, branch_id, seq_in_branch, plugin_id, patch_type, patch_data, tenant_id, created_at) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9)",
            rusqlite::params![
                entry.trace_id,
                entry.run_id,
                entry.branch_id,
                entry.seq_in_branch,
                entry.plugin_id,
                patch_type_str,
                patch_data_str,
                tenant_id,
                entry.created_at,
            ],
        )?;
        Ok(())
    }

    fn update_run_status_inner(
        &self,
        run_id: &str,
        tenant_id: &str,
        status: RunStatus,
        current_branch: Option<&str>,
        current_seq: Option<u32>,
    ) -> Result<(), StorageError> {
        let conn = self.conn.lock();
        let status_str = match status {
            RunStatus::Running => "running",
            RunStatus::Suspended => "suspended",
            RunStatus::Completed => "completed",
            RunStatus::Failed => "failed",
            RunStatus::Cancelled => "cancelled",
        };
        let now = chrono::Utc::now().to_rfc3339();

        match (current_branch, current_seq) {
            (Some(branch), Some(seq)) => {
                let ended = if matches!(
                    status,
                    RunStatus::Completed | RunStatus::Failed | RunStatus::Cancelled
                ) {
                    Some(now.as_str())
                } else {
                    None
                };
                conn.execute(
                    "UPDATE runs SET status = ?1, current_branch = ?2, current_seq = ?3, ended_at = COALESCE(?4, ended_at) WHERE run_id = ?5 AND tenant_id = ?6",
                    rusqlite::params![status_str, branch, seq as i64, ended, run_id, tenant_id],
                )?;
            }
            (None, None) => {
                let ended = if matches!(
                    status,
                    RunStatus::Completed | RunStatus::Failed | RunStatus::Cancelled
                ) {
                    Some(now.as_str())
                } else {
                    None
                };
                conn.execute(
                    "UPDATE runs SET status = ?1, ended_at = COALESCE(?2, ended_at) WHERE run_id = ?3 AND tenant_id = ?4",
                    rusqlite::params![status_str, ended, run_id, tenant_id],
                )?;
            }
            _ => {
                return Err(StorageError::Database(
                    "current_branch and current_seq must both be Some or both be None".to_string(),
                ));
            }
        }
        Ok(())
    }

    /// 消费 refresh token jti（api auth D12 单次轮换的持久账本，跨重启判已消费）。
    ///
    /// 返回 true = 首次消费（token 有效）；false = 已消费过（旧值复用，调用方拒绝）。
    /// 写入时顺手清理保留窗口（`CONSUMED_REFRESH_JTI_TTL_SECS`，与 refresh token
    /// 有效期上限对齐）外的旧行。经 [`SqliteStore::with_conn`] 同步执行——消费点
    /// 是低频路径（每 token 至多一次），不入 DB 线程池。
    pub fn consume_refresh_jti(&self, jti: &str) -> Result<bool, StorageError> {
        self.with_conn(|conn| {
            let now = chrono::Utc::now().timestamp();
            conn.execute(
                "DELETE FROM consumed_refresh_jtis WHERE consumed_at < ?1",
                rusqlite::params![now - CONSUMED_REFRESH_JTI_TTL_SECS as i64],
            )?;
            let inserted = conn.execute(
                "INSERT OR IGNORE INTO consumed_refresh_jtis (jti, consumed_at) VALUES (?1, ?2)",
                rusqlite::params![jti, now],
            )?;
            Ok(inserted == 1)
        })
    }

    /// 在锁内执行任意 SQLite 操作（统一数据接口 `/api/v1/db/*` 专用）。
    ///
    /// 暴露只读的 `&Connection` 给上层做表驱动动态访问（`sqlite_master` /
    /// `PRAGMA table_info` / 通用查询/CRUD）。不改变任何持久化语义——
    /// 只是连接访问的受控出口，DDL/迁移逻辑仍由本模块独占。
    ///
    /// 错误类型 `E` 由调用方决定（如 api 层的 `ApiError`），锁获取本身不失败。
    pub fn with_conn<T, E>(&self, f: impl FnOnce(&Connection) -> Result<T, E>) -> Result<T, E> {
        let conn = self.conn.lock();
        f(&conn)
    }

    /// 统一阻塞执行包装：参数先转为 owned 再随闭包移入固定长命 DB 线程池，
    /// 同步 `_inner` 方法在池内拿 `&SqliteStore` 执行，async 侧 await oneshot 回执。
    ///
    /// SqliteStore 用同步 rusqlite，trait 面 async 包装必须把阻塞调用移出
    /// async runtime 线程。用固定线程池而非阻塞池短命线程：每轮每查询换手
    /// 线程会让 mimalloc per-thread 堆随线程废弃滞留（空闲即销毁重建），
    /// 分配密集负载下内存随换手次数增长——固定线程复用同一堆，换手归零。
    /// 闭包内的同名词方法解析遵循固有方法优先。
    async fn blocking<F, T>(&self, f: F) -> Result<T, StorageError>
    where
        F: FnOnce(&Self) -> Result<T, StorageError> + Send + 'static,
        T: Send + 'static,
    {
        let this = self.clone();
        let (ack_tx, ack_rx) = tokio::sync::oneshot::channel();
        self.pool.dispatch(Box::new(move || {
            // 发送失败 = 调用侧 future 已被 drop（取消）：结果就地丢弃，
            // 与阻塞池任务完成后调用方不再等待的既有语义一致。
            let _ = ack_tx.send(f(&this));
        }))?;
        // 回执缺失 = 任务在产出结果前消亡（任务 panic 已被池隔离为 warn 留痕，
        // 展开时丢弃回执通道）——映射为错误，不让调用方悬挂。
        ack_rx.await.map_err(|_| {
            StorageError::Database(
                "db pool task ended without a receipt (task panicked)".to_string(),
            )
        })?
    }

    // ── 保留策略：traces 保留期 + blobs 孤儿清扫（ADR 2026-09-11）────────
    //
    // 账本域离线家政：traces 保留窗口外的行、不被 message_slots 引用的孤儿
    // blob，批量 DELETE 周期回收（内核启动后清一次 + 每 6h，接线见
    // agentos-kernel）。与 delete_pipelines_cascade（删除热路径）刻意分离——
    // blobs 是内容寻址去重存储，逐次删除时判定引用归属等于热路径写放大，
    // 孤儿统一留待清扫周期廉价回收。保留天数见 [`trace_retention_days`]。

    /// 删除 created_at 早于 cutoff 的 traces 行，返回删除行数（0 = 无过期行）。
    ///
    /// created_at 由写入方统一以 `chrono::Utc::now().to_rfc3339()`（UTC
    /// RFC3339）落库，cutoff 同格式化后字典序 = 时间序，无需日期解析函数。
    /// 单条 DELETE 自成原子事务；traces 无 created_at 索引（热路径写免维护），
    /// 清扫是周期批处理，全表扫描代价可接受。
    pub async fn purge_traces_older_than(
        &self,
        cutoff: chrono::DateTime<chrono::Utc>,
    ) -> Result<u64, StorageError> {
        let cutoff = cutoff.to_rfc3339();
        self.blocking(move |this| {
            let conn = this.conn.lock();
            let deleted = conn.execute(
                "DELETE FROM traces WHERE created_at < ?1",
                rusqlite::params![cutoff],
            )?;
            Ok(deleted as u64)
        })
        .await
    }

    /// 清扫孤儿 blobs：删除不被任何 message_slots 行引用的 blob，返回删除行数。
    ///
    /// blobs 唯一引用方 = message_slots.blob_id（消息全文内容寻址去重存储，
    /// 全仓写入点均在消息持久化路径），NOT EXISTS 反查无回指即孤儿；槽位
    /// blob_id 可空，空值不构成引用（NULL 比较不命中）。单条 DELETE 自成
    /// 原子事务。
    pub async fn purge_orphan_blobs(&self) -> Result<u64, StorageError> {
        self.blocking(move |this| {
            let conn = this.conn.lock();
            let deleted = conn.execute(
                "DELETE FROM blobs WHERE NOT EXISTS \
                 (SELECT 1 FROM message_slots WHERE message_slots.blob_id = blobs.blob_id)",
                [],
            )?;
            Ok(deleted as u64)
        })
        .await
    }
}

/// 按 thread 收集会话关联的 pipeline_id 去重集合：`pipeline_sessions` 映射表
/// 为主，`sessions.pipeline_ids`（JSON 文本）兜底补全——防映射表未落行时漏掉
/// 主管道。读路径/删除路径共用。
fn pipeline_ids_for_thread(
    conn: &rusqlite::Connection,
    thread_id: &str,
    tenant_id: &str,
) -> Result<Vec<String>, StorageError> {
    let mut pipeline_ids: Vec<String> = Vec::new();
    {
        let mut stmt = conn.prepare(
            "SELECT pipeline_id FROM pipeline_sessions WHERE thread_id = ?1 AND tenant_id = ?2",
        )?;
        let rows = stmt.query_map(rusqlite::params![thread_id, tenant_id], |row| {
            row.get::<_, String>(0)
        })?;
        for r in rows {
            let pid = r?;
            if !pipeline_ids.contains(&pid) {
                pipeline_ids.push(pid);
            }
        }
    }
    // 兜底：从 sessions.pipeline_ids (JSON) 补全主管道 id
    let session_row: Option<(Option<String>,)> = conn
        .query_row(
            "SELECT pipeline_ids FROM sessions WHERE thread_id = ?1 AND tenant_id = ?2",
            rusqlite::params![thread_id, tenant_id],
            |row| Ok((row.get::<_, Option<String>>(0)?,)),
        )
        .optional()?;
    if let Some((Some(json),)) = session_row {
        let list = serde_json::from_str::<Vec<String>>(&json).map_err(|e| {
            StorageError::Serialization(format!("session {thread_id} pipeline_ids: {e}"))
        })?;
        for pid in list {
            if !pid.is_empty() && !pipeline_ids.contains(&pid) {
                pipeline_ids.push(pid);
            }
        }
    }
    Ok(pipeline_ids)
}

/// 沿血缘 `lineage.parent_pipeline_id`（子→父指针）收集根管道集的全部后代
/// 任务管道，BFS 逐层展开（防环 visited）。返回不含 roots 自身、按发现序排列。
///
/// 删除面专用：子任务管道出生落自环 pipeline_sessions 绑定（thread=自身 id，
/// sessions 表无行），不属于任何被删会话——删除清单不沿血缘补全就会整链漏删
/// （runs/state/绑定残留，管理页幽灵条目）。field_value 是 JSON 编码标量
/// （字符串带引号），解析后比对，解析失败按原值兜底。
fn descendant_pipeline_ids(
    conn: &rusqlite::Connection,
    roots: &[String],
    tenant_id: &str,
) -> Result<Vec<String>, StorageError> {
    if roots.is_empty() {
        return Ok(Vec::new());
    }
    let mut stmt = conn.prepare(
        "SELECT pipeline_id, field_value FROM pipeline_state \
         WHERE tenant_id = ?1 AND field_key = 'lineage.parent_pipeline_id'",
    )?;
    let rows = stmt.query_map(rusqlite::params![tenant_id], |row| {
        Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
    })?;
    let mut children_by_parent: std::collections::HashMap<String, Vec<String>> =
        std::collections::HashMap::new();
    for r in rows {
        let (child, raw) = r?;
        let parent: String = serde_json::from_str(&raw).unwrap_or_else(|_| raw.clone());
        children_by_parent.entry(parent).or_default().push(child);
    }
    let mut out: Vec<String> = Vec::new();
    let mut seen: std::collections::HashSet<String> = roots.iter().cloned().collect();
    let mut frontier: Vec<String> = roots.to_vec();
    while let Some(pid) = frontier.pop() {
        if let Some(children) = children_by_parent.get(&pid) {
            for child in children {
                if seen.insert(child.clone()) {
                    out.push(child.clone());
                    frontier.push(child.clone());
                }
            }
        }
    }
    Ok(out)
}

/// 经 message_slots 按 pipeline 集合反查产生过的 run_id 去重集合，跳过 NULL。
///
/// `message_slots.run_id` 可为 NULL（流式占位消息等）：用 Option 读取跳过，
/// 避免 Invalid column type Null 抛错导致整个删除事务回滚。
/// 调用方需保证 `pipeline_ids` 非空（空 IN 列表非法；现有调用点均先空集早退）。
fn run_ids_of_pipelines(
    conn: &rusqlite::Connection,
    pipeline_ids: &[String],
    tenant_id: &str,
) -> Result<Vec<String>, StorageError> {
    let placeholders = (0..pipeline_ids.len())
        .map(|i| format!("?{}", i + 1))
        .collect::<Vec<_>>()
        .join(", ");
    let sql = format!(
        "SELECT DISTINCT run_id FROM message_slots WHERE pipeline_id IN ({placeholders}) AND tenant_id = ?"
    );
    let mut stmt = conn.prepare(&sql)?;
    let mut params: Vec<&dyn rusqlite::ToSql> = pipeline_ids
        .iter()
        .map(|p| p as &dyn rusqlite::ToSql)
        .collect();
    params.push(&tenant_id);
    let rows = stmt.query_map(params.as_slice(), |row| row.get::<_, Option<String>>(0))?;
    let mut out = Vec::new();
    for r in rows {
        if let Some(rid) = r? {
            if !out.contains(&rid) {
                out.push(rid);
            }
        }
    }
    Ok(out)
}

/// 池任务：自带回执通道的闭包（调用侧 [`SqliteStore::blocking`] 打包 oneshot
/// Sender，闭包执行完把结果发回 async 侧）。
type DbJob = Box<dyn FnOnce() + Send + 'static>;

/// 多工作线程共享的任务队列接收端（取任务串行化，执行在锁外）。
type DbJobQueue = Mutex<std::sync::mpsc::Receiver<DbJob>>;

/// 工作线程主循环：从共享队列竞争取任务执行，直到队列关闭且排空。
fn db_pool_worker_loop(queue: Arc<DbJobQueue>) {
    loop {
        // 锁只护取任务；Guard 在语句末释放，任务执行不持锁。
        let job = queue.lock().recv();
        match job {
            Ok(job) => {
                // panic 隔离：单个任务 panic 不许打死长命线程（池容量恒定，
                // 与原阻塞池 per-task 隔离等价）；回执通道随展开丢弃，
                // async 侧据此收到错误而非悬挂。
                if std::panic::catch_unwind(std::panic::AssertUnwindSafe(job)).is_err() {
                    warn!("db pool task panicked; worker thread survives");
                }
            }
            // 发送端全部关闭（池已 Drop）且队列已排空：长命线程自然退出。
            Err(_) => break,
        }
    }
}

/// 固定长命 DB 工作线程池（SqliteStore 的专属执行域）。
///
/// 为什么固定线程：async 面的同步 rusqlite 调用若经阻塞池（spawn_blocking）
/// 执行，每轮每查询可能落在不同短命线程上——池空闲即销毁线程、新任务再建，
/// mimalloc 的 per-thread 堆随线程废弃滞留不归还，分配密集负载下内存随线程
/// 换手次数增长（同负载背靠背轮次 3 倍实证）。固定 4 条长命线程 + 任务队列：
/// 线程与 store 同寿命，per-thread 堆持续复用不废弃，线程换手归零。
///
/// 生命周期契约：
/// - 随 [`SqliteStore`] 创建而启动，clone 共享同一池（Arc 计数）；
/// - 随最后一份 store 消亡（本池 Drop → 发送端关闭）而关停——std mpsc 保证
///   工作线程先排空已入队任务再退出，关停不丢任务；
/// - 线程 detach、不 join：任务闭包本身持有 store clone（含本池 Arc），最后
///   一份 Arc 可能在工作线程上随闭包 Drop，此时 join 自身会死锁。
struct DedicatedDbPool {
    /// 任务队列发送端（有界）。Drop 本字段即触发关停（channel 关闭 → 线程
    /// 排空后退出）。
    tx: std::sync::mpsc::SyncSender<DbJob>,
    /// 队列满触发背压（dispatch 阻塞）的累计次数：过载可观测，不静默。
    blocked_dispatches: std::sync::atomic::AtomicU64,
}

impl DedicatedDbPool {
    /// 长命工作线程数。单连接在 Mutex 后串行执行，多线程用于并行派发与
    /// 抗个别任务阻塞（维持与原阻塞池相当的并发上限）。
    const WORKER_THREADS: usize = 4;

    /// 队列容量（有界）：DB 过载时 dispatch 阻塞等待（背压传导到调用方），
    /// 不再无界积压闭包——待执行任务内存上限恒为容量 × 单闭包大小。
    const QUEUE_CAPACITY: usize = 1024;

    fn spawn() -> Result<Self, StorageError> {
        Self::spawn_with(Self::WORKER_THREADS, Self::QUEUE_CAPACITY)
    }

    /// 以指定线程数/队列容量启动（生产用 [`Self::spawn`] 常量；测试注入
    /// 小容量以确定性触发背压路径）。
    fn spawn_with(worker_threads: usize, queue_capacity: usize) -> Result<Self, StorageError> {
        let (tx, rx) = std::sync::mpsc::sync_channel::<DbJob>(queue_capacity);
        let queue = Arc::new(DbJobQueue::new(rx));
        for i in 0..worker_threads {
            let queue = Arc::clone(&queue);
            std::thread::Builder::new()
                .name(format!("agentos-db-pool-{i}"))
                .spawn(move || db_pool_worker_loop(queue))
                .map_err(|e| StorageError::Database(format!("spawn db pool worker: {e}")))?;
        }
        Ok(Self {
            tx,
            blocked_dispatches: std::sync::atomic::AtomicU64::new(0),
        })
    }

    /// 入队一个任务。队列满即阻塞派发方（背压，warn 计数留痕不静默）；
    /// 池已关停（Drop）后拒绝并返回错误，不再接受新任务。
    fn dispatch(&self, job: DbJob) -> Result<(), StorageError> {
        match self.tx.try_send(job) {
            Ok(()) => Ok(()),
            Err(std::sync::mpsc::TrySendError::Full(job)) => {
                let blocked = self
                    .blocked_dispatches
                    .fetch_add(1, std::sync::atomic::Ordering::Relaxed)
                    + 1;
                warn!(
                    blocked_dispatches = blocked,
                    "db queue full; dispatch blocking (backpressure)"
                );
                self.tx.send(job).map_err(|_| {
                    StorageError::Database("db worker pool shut down; task rejected".to_string())
                })
            }
            Err(std::sync::mpsc::TrySendError::Disconnected(_)) => Err(StorageError::Database(
                "db worker pool shut down; task rejected".to_string(),
            )),
        }
    }
}

#[async_trait]
impl StorageBackend for SqliteStore {
    // 热路径六方法（get_run/set_run_pipeline/list_runs_by_pipeline/get_blob/
    // append_trace/update_run_status）统一经 blocking() 派发专用 DB 线程池，
    // 不在 tokio worker 上直接 conn.lock()（同步体见固有 impl 的 *_inner）。

    async fn get_run(&self, run_id: &str) -> Result<RunRecord, StorageError> {
        // task_local 租户只在调用方 task 存在（池线程无 task_local）：
        // 必须在派发前读取，随闭包带入池内。
        let tenant_id = agentos_tenant::current_or_default("default").tenant_id;
        let run_id = run_id.to_string();
        self.blocking(move |this| this.get_run_inner(&run_id, &tenant_id))
            .await
    }

    async fn set_run_pipeline(&self, run_id: &str, pipeline_id: &str) -> Result<(), StorageError> {
        let run_id = run_id.to_string();
        let pipeline_id = pipeline_id.to_string();
        self.blocking(move |this| this.set_run_pipeline_inner(&run_id, &pipeline_id))
            .await
    }

    /// 管道运行快照列表（trait 面）：委托 list_pipelines_inner（同 HTTP /pipelines/runs）。
    async fn list_pipelines(
        &self,
        tenant_id: &str,
        status: Option<&str>,
        limit: u32,
    ) -> Result<Vec<PipelineRunInfo>, StorageError> {
        self.list_pipelines_inner(tenant_id, status, limit)
    }

    async fn list_runs_by_pipeline(
        &self,
        pipeline_id: &str,
        tenant_id: &str,
    ) -> Result<Vec<RunRecord>, StorageError> {
        let pipeline_id = pipeline_id.to_string();
        let tenant_id = tenant_id.to_string();
        self.blocking(move |this| this.list_runs_by_pipeline_inner(&pipeline_id, &tenant_id))
            .await
    }

    async fn get_messages_by_pipeline(
        &self,
        pipeline_id: &str,
        opts: MessageQueryOpts,
    ) -> Result<Vec<MessageRecord>, StorageError> {
        // 零兼容：历史读路径统一走 message_slots（纯索引 join blobs 读时重建），
        // 无 messages 投影表。接口形状（MessageRecord）不变，前端零改动。
        let tenant_id = agentos_tenant::current_or_default("default").tenant_id;
        self.get_slot_messages_by_pipeline(pipeline_id, &tenant_id, opts)
    }

    async fn get_blob(&self, blob_id: &str) -> Result<Vec<u8>, StorageError> {
        let blob_id = blob_id.to_string();
        self.blocking(move |this| this.get_blob_inner(&blob_id))
            .await
    }

    async fn append_trace(&self, entry: TraceEntry) -> Result<(), StorageError> {
        let tenant_id = agentos_tenant::current_or_default("default").tenant_id;
        self.blocking(move |this| this.append_trace_inner(entry, &tenant_id))
            .await
    }

    async fn update_run_status(
        &self,
        run_id: &str,
        status: RunStatus,
        current_branch: Option<&str>,
        current_seq: Option<u32>,
    ) -> Result<(), StorageError> {
        // task_local 租户只在调用方 task 存在（池线程无 task_local）：
        // 必须在派发前读取，随闭包带入池内。
        let tenant_id = agentos_tenant::current_or_default("default").tenant_id;
        let run_id = run_id.to_string();
        let current_branch = current_branch.map(str::to_string);
        self.blocking(move |this| {
            this.update_run_status_inner(
                &run_id,
                &tenant_id,
                status,
                current_branch.as_deref(),
                current_seq,
            )
        })
        .await
    }

    // 以下 trait 方法直接转发到固有 impl 的同名方法，
    // 让持 Arc<dyn StorageBackend> 的 PipelineExecutor 能调到写方法。
    async fn create_run(
        &self,
        run_id: &str,
        config_hash: &str,
        tenant_id: &str,
    ) -> Result<(), StorageError> {
        // 在阻塞任务里执行，避免阻塞 async runtime（SqliteStore 用同步 rusqlite）
        let run_id = run_id.to_string();
        let config_hash = config_hash.to_string();
        let tenant_id = tenant_id.to_string();
        self.blocking(move |this| this.create_run(&run_id, &config_hash, &tenant_id))
            .await
    }

    async fn store_blob(&self, data: &[u8], mime_type: &str) -> Result<String, StorageError> {
        let data = data.to_vec();
        let mime_type = mime_type.to_string();
        self.blocking(move |this| this.store_blob(&data, &mime_type))
            .await
    }

    // ── 域10：分层持久化投影（trait async 包装，DB 线程池派发 + task_local tenant）──

    async fn apply_messages_ops_to_table(
        &self,
        pipeline_id: &str,
        tenant_id: &str,
        ops: &[serde_json::Value],
    ) -> Result<(), StorageError> {
        let pipeline_id = pipeline_id.to_string();
        let tenant_id = tenant_id.to_string();
        let ops = ops.to_vec();
        self.blocking(move |this| this.apply_messages_ops_to_table(&pipeline_id, &tenant_id, &ops))
            .await
    }

    async fn upsert_state_field(
        &self,
        pipeline_id: &str,
        tenant_id: &str,
        key: &str,
        value: &serde_json::Value,
    ) -> Result<(), StorageError> {
        let pipeline_id = pipeline_id.to_string();
        let tenant_id = tenant_id.to_string();
        let key = key.to_string();
        let value = value.clone();
        self.blocking(move |this| this.upsert_state_field(&pipeline_id, &tenant_id, &key, &value))
            .await
    }

    async fn upsert_state_fields(
        &self,
        pipeline_id: &str,
        tenant_id: &str,
        fields: &serde_json::Map<String, serde_json::Value>,
    ) -> Result<(), StorageError> {
        let pipeline_id = pipeline_id.to_string();
        let tenant_id = tenant_id.to_string();
        let fields = fields.clone();
        self.blocking(move |this| this.upsert_state_fields(&pipeline_id, &tenant_id, &fields))
            .await
    }

    async fn load_pipeline_state(
        &self,
        pipeline_id: &str,
        tenant_id: &str,
    ) -> Result<std::collections::HashMap<String, serde_json::Value>, StorageError> {
        let pipeline_id = pipeline_id.to_string();
        let tenant_id = tenant_id.to_string();
        self.blocking(move |this| this.load_pipeline_state(&pipeline_id, &tenant_id))
            .await
    }

    fn set_declared_volatile_keys(&self, keys: &[String]) {
        *self.declared_volatile_keys.write() = keys.iter().cloned().collect();
    }

    async fn save_checkpoint(
        &self,
        pipeline_id: &str,
        tenant_id: &str,
        step_no: i64,
        state: &serde_json::Value,
    ) -> Result<(), StorageError> {
        let pipeline_id = pipeline_id.to_string();
        let tenant_id = tenant_id.to_string();
        let state = state.clone();
        self.blocking(move |this| this.save_checkpoint(&pipeline_id, &tenant_id, step_no, &state))
            .await
    }

    async fn load_latest_checkpoint(
        &self,
        pipeline_id: &str,
        tenant_id: &str,
    ) -> Result<Option<(i64, serde_json::Value)>, StorageError> {
        let pipeline_id = pipeline_id.to_string();
        let tenant_id = tenant_id.to_string();
        self.blocking(move |this| this.load_latest_checkpoint(&pipeline_id, &tenant_id))
            .await
    }

    async fn list_state_pipeline_ids(
        &self,
        tenant_id: &str,
    ) -> Result<Vec<(String, String)>, StorageError> {
        let tenant_id = tenant_id.to_string();
        self.blocking(move |this| this.list_state_pipeline_ids(&tenant_id))
            .await
    }

    async fn load_message_history(
        &self,
        pipeline_id: &str,
        tenant_id: &str,
    ) -> Result<Vec<serde_json::Value>, StorageError> {
        let pipeline_id = pipeline_id.to_string();
        let tenant_id = tenant_id.to_string();
        self.blocking(move |this| this.load_message_history(&pipeline_id, &tenant_id))
            .await
    }

    // ── 域11：pending 输入队列（ADR-2026-08-26）──────────────────────

    async fn enqueue_pending_input(
        &self,
        tenant_id: &str,
        pipeline_id: &str,
        input: &PendingInputRecord,
    ) -> Result<(), StorageError> {
        let tenant_id = tenant_id.to_string();
        let pipeline_id = pipeline_id.to_string();
        let input = input.clone();
        self.blocking(move |this| this.enqueue_pending_input(&tenant_id, &pipeline_id, &input))
            .await
    }

    async fn pop_pending_input(
        &self,
        tenant_id: &str,
        pipeline_id: &str,
    ) -> Result<Option<PendingInputRecord>, StorageError> {
        let tenant_id = tenant_id.to_string();
        let pipeline_id = pipeline_id.to_string();
        self.blocking(move |this| this.pop_pending_input(&tenant_id, &pipeline_id))
            .await
    }

    async fn list_pending_inputs(
        &self,
        tenant_id: &str,
        pipeline_id: &str,
    ) -> Result<Vec<PendingInputRecord>, StorageError> {
        let tenant_id = tenant_id.to_string();
        let pipeline_id = pipeline_id.to_string();
        self.blocking(move |this| this.list_pending_inputs(&tenant_id, &pipeline_id))
            .await
    }

    async fn update_pending_input_content(
        &self,
        tenant_id: &str,
        pipeline_id: &str,
        input_id: &str,
        new_content: &str,
    ) -> Result<bool, StorageError> {
        let tenant_id = tenant_id.to_string();
        let pipeline_id = pipeline_id.to_string();
        let input_id = input_id.to_string();
        let new_content = new_content.to_string();
        self.blocking(move |this| {
            this.update_pending_input_content(&tenant_id, &pipeline_id, &input_id, &new_content)
        })
        .await
    }

    async fn delete_pending_input(
        &self,
        tenant_id: &str,
        pipeline_id: &str,
        input_id: &str,
    ) -> Result<bool, StorageError> {
        let tenant_id = tenant_id.to_string();
        let pipeline_id = pipeline_id.to_string();
        let input_id = input_id.to_string();
        self.blocking(move |this| this.delete_pending_input(&tenant_id, &pipeline_id, &input_id))
            .await
    }

    async fn clear_pending_inputs(
        &self,
        tenant_id: &str,
        pipeline_id: &str,
    ) -> Result<usize, StorageError> {
        let tenant_id = tenant_id.to_string();
        let pipeline_id = pipeline_id.to_string();
        self.blocking(move |this| this.clear_pending_inputs(&tenant_id, &pipeline_id))
            .await
    }

    // ── 域2：session 标签夹 CRUD ──────────────
    // 注意：tenant_id 必须在派发到 DB 线程池之前解析——tokio::task_local 不跨池线程。
    async fn create_session(&self, session: &SessionRecord) -> Result<(), StorageError> {
        let tenant_id = agentos_tenant::current_or_default("default").tenant_id;
        let session = session.clone();
        self.blocking(move |this| this.upsert_session_inner(&session, &tenant_id))
            .await
    }

    async fn get_session(&self, thread_id: &str) -> Result<Option<SessionRecord>, StorageError> {
        let tenant_id = agentos_tenant::current_or_default("default").tenant_id;
        let thread_id = thread_id.to_string();
        self.blocking(move |this| this.get_session_inner(&thread_id, &tenant_id))
            .await
    }

    async fn list_sessions(
        &self,
        filter: SessionListFilter,
    ) -> Result<Vec<SessionRecord>, StorageError> {
        let tenant_id = agentos_tenant::current_or_default("default").tenant_id;
        self.blocking(move |this| this.list_sessions_inner(&filter, &tenant_id))
            .await
    }

    async fn update_session(&self, session: &SessionRecord) -> Result<(), StorageError> {
        let tenant_id = agentos_tenant::current_or_default("default").tenant_id;
        let session = session.clone();
        self.blocking(move |this| this.upsert_session_inner(&session, &tenant_id))
            .await
    }

    async fn delete_session(&self, thread_id: &str) -> Result<Vec<String>, StorageError> {
        let tenant_id = agentos_tenant::current_or_default("default").tenant_id;
        let thread_id = thread_id.to_string();
        self.blocking(move |this| this.delete_session_inner(&thread_id, &tenant_id))
            .await
    }

    async fn delete_pipeline(&self, pipeline_id: &str) -> Result<Vec<String>, StorageError> {
        let tenant_id = agentos_tenant::current_or_default("default").tenant_id;
        let pipeline_id = pipeline_id.to_string();
        self.blocking(move |this| this.delete_pipeline_inner(&pipeline_id, &tenant_id))
            .await
    }

    async fn link_pipeline_session(
        &self,
        pipeline_id: &str,
        thread_id: &str,
        tenant_id: &str,
    ) -> Result<(), StorageError> {
        let pipeline_id = pipeline_id.to_string();
        let thread_id = thread_id.to_string();
        let tenant_id = tenant_id.to_string();
        self.blocking(move |this| {
            this.link_pipeline_session_inner(&pipeline_id, &thread_id, &tenant_id)
        })
        .await
    }

    async fn list_pipeline_ids_by_thread(
        &self,
        thread_id: &str,
        tenant_id: &str,
    ) -> Result<Vec<String>, StorageError> {
        let thread_id = thread_id.to_string();
        let tenant_id = tenant_id.to_string();
        self.blocking(move |this| this.list_pipeline_ids_by_thread_inner(&thread_id, &tenant_id))
            .await
    }

    async fn get_thread_id_by_pipeline(
        &self,
        pipeline_id: &str,
    ) -> Result<Option<String>, StorageError> {
        let pipeline_id = pipeline_id.to_string();
        self.blocking(move |this| this.get_thread_id_by_pipeline_inner(&pipeline_id))
            .await
    }

    async fn get_step_traces_by_thread(
        &self,
        thread_id: &str,
        tenant_id: &str,
    ) -> Result<Vec<TraceEntry>, StorageError> {
        let thread_id = thread_id.to_string();
        let tenant_id = tenant_id.to_string();
        self.blocking(move |this| this.get_step_traces_by_thread_inner(&thread_id, &tenant_id))
            .await
    }

    async fn get_step_traces_by_pipeline(
        &self,
        pipeline_id: &str,
        tenant_id: &str,
    ) -> Result<Vec<TraceEntry>, StorageError> {
        let pipeline_id = pipeline_id.to_string();
        let tenant_id = tenant_id.to_string();
        self.blocking(move |this| this.get_step_traces_by_pipeline_inner(&pipeline_id, &tenant_id))
            .await
    }

    // ── 域6：users async wrapper（0.5.0 最小持久化地基）──────────────
    // 注意：get_user_by_username / list_users 跨租户查询，不解析 task_local tenant。
    // get_user_by_id 按 tenant 隔离（与消息/会话一致，task_local 在派发前解析）。

    async fn create_user(&self, user: &UserRecord) -> Result<(), StorageError> {
        let user = user.clone();
        self.blocking(move |this| this.create_user_inner(&user))
            .await
    }

    async fn get_user_by_id(&self, user_id: &str) -> Result<Option<UserRecord>, StorageError> {
        // 跨租户查询（token 解析场景，user_id 是全局主键），不依赖 task_local tenant
        let user_id = user_id.to_string();
        self.blocking(move |this| this.get_user_by_id_inner(&user_id))
            .await
    }

    async fn get_user_by_username(
        &self,
        username: &str,
    ) -> Result<Option<UserRecord>, StorageError> {
        // 跨租户查询（登录时还没有租户上下文），不解析 task_local tenant
        let username = username.to_string();
        self.blocking(move |this| this.get_user_by_username_inner(&username))
            .await
    }

    async fn list_users(&self) -> Result<Vec<UserRecord>, StorageError> {
        self.blocking(move |this| this.list_users_inner()).await
    }

    async fn update_last_login(&self, user_id: &str) -> Result<(), StorageError> {
        let user_id = user_id.to_string();
        self.blocking(move |this| this.update_last_login_inner(&user_id))
            .await
    }

    async fn update_user_password(
        &self,
        user_id: &str,
        password_hash: &str,
        must_change_password: bool,
    ) -> Result<bool, StorageError> {
        let user_id = user_id.to_string();
        let password_hash = password_hash.to_string();
        self.blocking(move |this| {
            this.update_user_password_inner(&user_id, &password_hash, must_change_password)
        })
        .await
    }

    async fn delete_user(&self, user_id: &str) -> Result<bool, StorageError> {
        let user_id = user_id.to_string();
        self.blocking(move |this| this.delete_user_inner(&user_id))
            .await
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use agentos_core::types::TenantContext;
    use serde_json::json;

    #[test]
    fn test_open_memory_stamps_schema_version() {
        let store = SqliteStore::open_memory().unwrap();
        let v: i64 = store
            .conn
            .lock()
            .query_row("PRAGMA user_version", [], |r| r.get(0))
            .unwrap();
        assert_eq!(v, SCHEMA_VERSION, "打开即盖版本章");
    }

    #[test]
    fn test_open_rejects_database_from_newer_kernel() {
        let dir = tempfile::tempdir().unwrap();
        let db_path = dir.path().join("newer.db");
        {
            let conn = rusqlite::Connection::open(&db_path).unwrap();
            conn.execute_batch("PRAGMA user_version = 99;").unwrap();
        }
        let err = match SqliteStore::open(db_path.to_str().unwrap()) {
            Err(e) => e,
            Ok(_) => panic!("新版本库必须被拒绝打开"),
        };
        let msg = err.to_string();
        assert!(
            msg.contains("schema 版本") && msg.contains("99"),
            "必须显式拒绝新版本库并带版本号: {msg}"
        );
    }

    #[test]
    fn test_open_file_store_idempotent_reopen() {
        let dir = tempfile::tempdir().unwrap();
        let db_path = dir.path().join("legacy.db");
        SqliteStore::open(db_path.to_str().unwrap()).unwrap();
        let store = SqliteStore::open(db_path.to_str().unwrap()).unwrap();
        let v: i64 = store
            .conn
            .lock()
            .query_row("PRAGMA user_version", [], |r| r.get(0))
            .unwrap();
        assert_eq!(v, SCHEMA_VERSION, "重开幂等，版本章不被回退");
    }

    #[test]
    fn test_parse_patch_type_unknown_degrades_to_state_update() {
        // 未知 patch_type 静默归 StateUpdate 是既有语义（新引擎写入方
        // 全部命中已知值），但必须留痕——抽取为独立函数后未知值 warn 可见；
        // 行为契约不变：未知值仍落 StateUpdate（不透传错误破坏冷恢复）。
        assert_eq!(
            parse_patch_type("state_update", "tool_1"),
            PatchType::StateUpdate
        );
        assert_eq!(
            parse_patch_type("route_signal", "tool_1"),
            PatchType::RouteSignal
        );
        assert_eq!(parse_patch_type("error", "tool_1"), PatchType::Error);
        assert_eq!(
            parse_patch_type("lifecycle", "tool_1"),
            PatchType::Lifecycle
        );
        assert_eq!(parse_patch_type("rollback", "tool_1"), PatchType::Rollback);
        assert_eq!(
            parse_patch_type("unknown_future_type", "tool_1"),
            PatchType::StateUpdate
        );
    }

    #[test]
    fn test_parse_run_status_known_and_unknown() {
        // 已知值逐一映射；未知值按 Running 处理（读取路径不可 fail）但契约
        // 是 warn 留痕（写入方新增状态必须登记），与 parse_patch_type 同标准。
        assert_eq!(parse_run_status("running", "r1"), RunStatus::Running);
        assert_eq!(parse_run_status("suspended", "r1"), RunStatus::Suspended);
        assert_eq!(parse_run_status("completed", "r1"), RunStatus::Completed);
        assert_eq!(parse_run_status("failed", "r1"), RunStatus::Failed);
        assert_eq!(parse_run_status("cancelled", "r1"), RunStatus::Cancelled);
        assert_eq!(
            parse_run_status("unknown_future_status", "r1"),
            RunStatus::Running
        );
    }

    #[test]
    fn test_parse_run_metadata_valid_corrupt_and_absent() {
        // 合法 JSON 原样解析；缺失(None)与损坏 JSON 同落 None，但损坏必须
        // warn 留痕——审批挂起凭据静默消失会让恢复链查不到 run 且无痕迹。
        let ok = parse_run_metadata(Some("{\"k\":1}".to_string()), "r1").expect("合法 JSON 应解析");
        assert_eq!(ok["k"], 1);
        assert!(parse_run_metadata(None, "r1").is_none(), "缺失按 None");
        assert!(
            parse_run_metadata(Some("{not-json".to_string()), "r1").is_none(),
            "损坏 JSON 按缺失处理（warn 留痕）"
        );
        // JSON null 合法解析为 Value::Null：消费面 m.get(...) 对 Null 同样取不到键，
        // 与缺失同效，无需特判。
        assert_eq!(
            parse_run_metadata(Some("null".to_string()), "r1"),
            Some(serde_json::Value::Null)
        );
    }

    #[test]
    fn test_extract_content_string() {
        // 字符串 content 原样透传
        assert_eq!(extract_content_string(&json!({"content": "你好"})), "你好");
        // 多 part：仅拼接 text/thinking，按出现顺序 \n 连接；其余 part 与
        // 非字符串 text 丢弃
        let multi = json!({"content": [
            {"type": "thinking", "text": "内心"},
            {"type": "text", "text": "正文一"},
            {"type": "text", "text": null},
            {"type": "tool_use", "id": "t1"},
            {"type": "text", "text": "正文二"}
        ]});
        assert_eq!(extract_content_string(&multi), "内心\n正文一\n正文二");
        // 性质：拼接段数 == text/thinking 且 text 为字符串的 part 数
        let kept: Vec<&serde_json::Value> = multi["content"]
            .as_array()
            .unwrap()
            .iter()
            .filter(|p| {
                matches!(
                    p.get("type").and_then(|v| v.as_str()),
                    Some("text" | "thinking")
                ) && p.get("text").and_then(|v| v.as_str()).is_some()
            })
            .collect();
        let out = extract_content_string(&multi);
        assert_eq!(out.matches('\n').count(), kept.len() - 1);
        assert!(!out.contains("tool"));
        // 非 string/array 的 content 与缺失 content 均为空串
        assert_eq!(extract_content_string(&json!({"content": 42})), "");
        assert_eq!(extract_content_string(&json!({"role": "user"})), "");
    }

    #[test]
    fn test_open_memory() {
        let store = SqliteStore::open_memory().unwrap();
        // 验证表存在——插入一条 run
        store
            .create_run("test_run_1", "hash_abc", "default")
            .unwrap();
    }

    /// 回归测试：delete_session 遇到 run_id 为 NULL 的 message_slots 行不抛错、
    /// 级联删除完整生效。
    ///
    /// 收集 run_ids 时 `row.get::<_, String>(0)` 遇 NULL 抛
    /// "Invalid column type Null" → 事务回滚 → 会话/消息/执行记录全部残留
    /// （DELETE /api/v1/sessions 返回 200 但啥也没删）。
    #[tokio::test]
    async fn test_delete_session_with_null_run_id_cascades() {
        let store = SqliteStore::open_memory().unwrap();
        let tid = "thread-delete-test";
        let pid = "pipeline-delete-test";
        let now = chrono::Utc::now().to_rfc3339();

        {
            let conn = store.conn.lock();
            conn.execute(
                "INSERT INTO sessions (thread_id, title, current_state, tenant_id, created_at, updated_at)                  VALUES (?1, ?2, 'active', 'default', ?3, ?3)",
                rusqlite::params![tid, "delete-test", now],
            )
            .unwrap();
            conn.execute(
                "INSERT INTO pipeline_sessions (pipeline_id, thread_id, tenant_id, created_at)                  VALUES (?1, ?2, 'default', ?3)",
                rusqlite::params![pid, tid, now],
            )
            .unwrap();
            // run_id NULL 的流式占位消息（bug 触发行）
            conn.execute(
                "INSERT INTO message_slots (tenant_id, pipeline_id, seq, message_id, run_id, created_at)                  VALUES ('default', ?1, 1, 'm-null-run', NULL, ?2)",
                rusqlite::params![pid, now],
            )
            .unwrap();
            // 有 run_id 的消息 + 对应 run/trace（应被级联删除）
            conn.execute(
                "INSERT INTO message_slots (tenant_id, pipeline_id, seq, message_id, run_id, created_at)                  VALUES ('default', ?1, 2, 'm-with-run', 'run-del-1', ?2)",
                rusqlite::params![pid, now],
            )
            .unwrap();
            conn.execute(
                "INSERT INTO runs (run_id, config_hash, status, tenant_id, created_at, current_branch)                  VALUES ('run-del-1', 'h', 'completed', 'default', ?1, 'main')",
                rusqlite::params![now],
            )
            .unwrap();
            conn.execute(
                "INSERT INTO traces (trace_id, run_id, branch_id, seq_in_branch, plugin_id, patch_type, patch_data, created_at)                  VALUES ('t1', 'run-del-1', 'main', 0, 'p', 'state', '{}', ?1)",
                rusqlite::params![now],
            )
            .unwrap();
        }

        // run_id 为 NULL 时不得抛 Invalid column type Null 导致事务回滚、全部残留
        store.delete_session(tid).await.unwrap();

        let conn = store.conn.lock();
        let count = |sql: &str| -> i64 { conn.query_row(sql, [], |r| r.get::<_, i64>(0)).unwrap() };
        assert_eq!(count("SELECT COUNT(*) FROM sessions"), 0, "sessions 应删除");
        assert_eq!(
            count("SELECT COUNT(*) FROM pipeline_sessions"),
            0,
            "映射应删除"
        );
        assert_eq!(count("SELECT COUNT(*) FROM message_slots"), 0, "消息应删除");
        assert_eq!(count("SELECT COUNT(*) FROM runs"), 0, "runs 应删除");
        assert_eq!(count("SELECT COUNT(*) FROM traces"), 0, "traces 应删除");
    }

    /// 回归测试：delete_session 沿血缘 lineage.parent_pipeline_id 级联子任务链。
    ///
    /// 子任务管道出生落自环绑定（thread=自身 id，sessions 表无行），按 thread
    /// 清单级联收集不到——不补血缘就会整链残留（runs/state/绑定），管理页出
    /// 幽灵条目。断言：子/孙管道全部数据表清零，返回清单含根+全部后代，
    /// 无关会话的管道数据不受波及。
    #[tokio::test]
    async fn test_delete_session_cascades_lineage_subtasks() {
        let store = SqliteStore::open_memory().unwrap();
        let tid = "thread-lineage-main";
        let now = chrono::Utc::now().to_rfc3339();

        {
            let conn = store.conn.lock();
            // 根会话 + 主管道绑定
            conn.execute(
                "INSERT INTO sessions (thread_id, title, current_state, tenant_id, created_at, updated_at) \
                 VALUES (?1, 'main', 'active', 'default', ?2, ?2)",
                rusqlite::params![tid, now],
            )
            .unwrap();
            conn.execute(
                "INSERT INTO pipeline_sessions (pipeline_id, thread_id, tenant_id, created_at) \
                 VALUES ('main-pipe', ?1, 'default', ?2)",
                rusqlite::params![tid, now],
            )
            .unwrap();
            // 子/孙管道：自环绑定（thread=自身 id，不指向任何会话行）
            for pid in ["sub-pipe", "grandchild-pipe"] {
                conn.execute(
                    "INSERT INTO pipeline_sessions (pipeline_id, thread_id, tenant_id, created_at) \
                     VALUES (?1, ?1, 'default', ?2)",
                    rusqlite::params![pid, now],
                )
                .unwrap();
            }
            // 血缘指针：grandchild -> sub -> main（子→父，JSON 编码标量）
            // 孙管道有完整执行数据（run/消息/state/checkpoint），应随会话整链删除
            conn.execute(
                "INSERT INTO message_slots (tenant_id, pipeline_id, seq, message_id, run_id, created_at) \
                 VALUES ('default', 'grandchild-pipe', 1, 'm-gc', 'run-gc', ?1)",
                rusqlite::params![now],
            )
            .unwrap();
            conn.execute(
                "INSERT INTO runs (run_id, config_hash, status, tenant_id, created_at, current_branch) \
                 VALUES ('run-gc', 'h', 'completed', 'default', ?1, 'main')",
                rusqlite::params![now],
            )
            .unwrap();
            conn.execute(
                "INSERT INTO pipeline_checkpoints (pipeline_id, tenant_id, step_no, state_json, created_at) \
                 VALUES ('grandchild-pipe', 'default', 1, '{}', ?1)",
                rusqlite::params![now],
            )
            .unwrap();
            // 无关会话 + 其管道（隔离对照：血缘链删除不得波及）
            conn.execute(
                "INSERT INTO sessions (thread_id, title, current_state, tenant_id, created_at, updated_at) \
                 VALUES ('thread-other', 'other', 'active', 'default', ?1, ?1)",
                rusqlite::params![now],
            )
            .unwrap();
            conn.execute(
                "INSERT INTO pipeline_sessions (pipeline_id, thread_id, tenant_id, created_at) \
                 VALUES ('other-pipe', 'thread-other', 'default', ?1)",
                rusqlite::params![now],
            )
            .unwrap();
        }
        // 血缘行经真实写面落库（编码契约：field_value 为 JSON 标量）
        store
            .upsert_state_field(
                "sub-pipe",
                "default",
                "lineage.parent_pipeline_id",
                &serde_json::json!("main-pipe"),
            )
            .unwrap();
        store
            .upsert_state_field(
                "grandchild-pipe",
                "default",
                "lineage.parent_pipeline_id",
                &serde_json::json!("sub-pipe"),
            )
            .unwrap();
        store
            .upsert_state_field(
                "grandchild-pipe",
                "default",
                "task.id",
                &serde_json::json!("grandchild-pipe"),
            )
            .unwrap();

        let deleted = store.delete_session(tid).await.unwrap();
        assert_eq!(deleted.len(), 3, "应删根+子+孙三个管道");
        for pid in ["main-pipe", "sub-pipe", "grandchild-pipe"] {
            assert!(deleted.iter().any(|d| d == pid), "清单缺 {pid}");
        }

        let conn = store.conn.lock();
        let count = |sql: &str| -> i64 { conn.query_row(sql, [], |r| r.get::<_, i64>(0)).unwrap() };
        assert_eq!(count("SELECT COUNT(*) FROM sessions"), 1, "无关会话应保留");
        assert_eq!(
            count("SELECT COUNT(*) FROM pipeline_sessions"),
            1,
            "仅无关管道绑定应保留"
        );
        assert_eq!(count("SELECT COUNT(*) FROM runs"), 0, "孙管道 run 应删除");
        assert_eq!(
            count("SELECT COUNT(*) FROM message_slots"),
            0,
            "孙管道消息应删除"
        );
        assert_eq!(
            count("SELECT COUNT(*) FROM pipeline_state"),
            0,
            "子/孙管道 state 行应删除"
        );
        assert_eq!(
            count("SELECT COUNT(*) FROM pipeline_checkpoints"),
            0,
            "孙管道 checkpoint 应删除"
        );
    }

    /// 回归测试：delete_session 在映射表无行时，经 sessions.pipeline_ids（JSON）
    /// 兜底收集照样级联清理；其他会话的管道数据不受波及。
    ///
    /// 正常输入（JSON 内两个管道 id）、边界（无 run_id 的占位消息行）与
    /// 隔离对照（第三管道归其他会话）三组断言共同锁定收集范围语义。
    #[tokio::test]
    async fn test_delete_session_collects_pipeline_ids_from_json_fallback() {
        let store = SqliteStore::open_memory().unwrap();
        let tid = "thread-json-fallback";
        let now = chrono::Utc::now().to_rfc3339();

        {
            let conn = store.conn.lock();
            // 会话行只带 pipeline_ids JSON，不写 pipeline_sessions 映射行（兜底唯一入口）
            conn.execute(
                "INSERT INTO sessions (thread_id, title, current_state, tenant_id, created_at, updated_at, pipeline_ids) \
                 VALUES (?1, ?2, 'active', 'default', ?3, ?3, ?4)",
                rusqlite::params![tid, "json-fallback", now, r#"["p-aaa","p-bbb"]"#],
            )
            .unwrap();
            // 兜底收集到的管道：有 run 的消息 → 应被级联删除
            conn.execute(
                "INSERT INTO message_slots (tenant_id, pipeline_id, seq, message_id, run_id, created_at) \
                 VALUES ('default', 'p-aaa', 1, 'm1', 'run-json-1', ?1)",
                rusqlite::params![now],
            )
            .unwrap();
            conn.execute(
                "INSERT INTO runs (run_id, config_hash, status, tenant_id, created_at, current_branch) \
                 VALUES ('run-json-1', 'h', 'completed', 'default', ?1, 'main')",
                rusqlite::params![now],
            )
            .unwrap();
            // 无 run_id 的流式占位行：跳过，不得抛错回滚
            conn.execute(
                "INSERT INTO message_slots (tenant_id, pipeline_id, seq, message_id, run_id, created_at) \
                 VALUES ('default', 'p-bbb', 1, 'm2-null', NULL, ?1)",
                rusqlite::params![now],
            )
            .unwrap();
            // 隔离对照：属于其他会话的管道，一个字节都不能少
            conn.execute(
                "INSERT INTO message_slots (tenant_id, pipeline_id, seq, message_id, run_id, created_at) \
                 VALUES ('default', 'p-other', 1, 'm3', 'run-json-keep', ?1)",
                rusqlite::params![now],
            )
            .unwrap();
            conn.execute(
                "INSERT INTO runs (run_id, config_hash, status, tenant_id, created_at, current_branch) \
                 VALUES ('run-json-keep', 'h', 'running', 'default', ?1, 'main')",
                rusqlite::params![now],
            )
            .unwrap();
        }

        store.delete_session(tid).await.unwrap();

        let conn = store.conn.lock();
        let count = |sql: &str| -> i64 { conn.query_row(sql, [], |r| r.get::<_, i64>(0)).unwrap() };
        assert_eq!(
            count("SELECT COUNT(*) FROM sessions"),
            0,
            "目标会话应删除（库中仅此一个会话）"
        );
        assert_eq!(
            count("SELECT COUNT(*) FROM runs WHERE run_id = 'run-json-1'"),
            0,
            "兜底收集的管道之 run 应级联删除"
        );
        assert_eq!(
            count("SELECT COUNT(*) FROM message_slots"),
            1,
            "应只剩隔离对照消息"
        );
        assert_eq!(
            count("SELECT COUNT(*) FROM message_slots WHERE message_id = 'm3'"),
            1,
            "其他会话的消息不应误删"
        );
        assert_eq!(
            count("SELECT COUNT(*) FROM runs WHERE run_id = 'run-json-keep'"),
            1,
            "其他会话的 run 不应误删"
        );
    }

    /// 回归测试：delete_session 级联必须覆盖 state 两表与 pending 输入队列
    /// （pipeline_state / pipeline_checkpoints / pipeline_pending_inputs）。
    ///
    /// 任务列表从 pipeline_state 的 `task.*` 键派生，pending 队列重启后被
    /// 消费续跑——任一残留都会让已删会话的任务以幽灵形态存活（重启也不
    /// 消失）。隔离对照：其他会话管道的同表行不得误删。
    #[tokio::test]
    async fn test_delete_session_cascades_state_and_pending_tables() {
        let store = SqliteStore::open_memory().unwrap();
        let tid = "thread-ghost-cascade";
        let now = chrono::Utc::now().to_rfc3339();

        {
            let conn = store.conn.lock();
            conn.execute(
                "INSERT INTO sessions (thread_id, title, current_state, tenant_id, created_at, updated_at) \
                 VALUES (?1, ?2, 'active', 'default', ?3, ?3)",
                rusqlite::params![tid, "ghost-cascade", now],
            )
            .unwrap();
            conn.execute(
                "INSERT INTO pipeline_sessions (pipeline_id, thread_id, tenant_id, created_at) \
                 VALUES ('p-ghost', ?1, 'default', ?2)",
                rusqlite::params![tid, now],
            )
            .unwrap();
            // 隔离对照：属于其他会话的管道
            conn.execute(
                "INSERT INTO pipeline_sessions (pipeline_id, thread_id, tenant_id, created_at) \
                 VALUES ('p-keep', 'thread-other', 'default', ?1)",
                rusqlite::params![now],
            )
            .unwrap();
            // 目标管道：state 标量 + checkpoint 快照 + pending 输入各一行（应全删）
            conn.execute(
                "INSERT INTO pipeline_state (tenant_id, pipeline_id, field_key, field_value, updated_at) \
                 VALUES ('default', 'p-ghost', 'task.status', 'running', ?1)",
                rusqlite::params![now],
            )
            .unwrap();
            conn.execute(
                "INSERT INTO pipeline_checkpoints (tenant_id, pipeline_id, checkpoint_id, step_no, state_json, created_at) \
                 VALUES ('default', 'p-ghost', 'cp-ghost', 0, '{}', ?1)",
                rusqlite::params![now],
            )
            .unwrap();
            conn.execute(
                "INSERT INTO pipeline_pending_inputs (id, pipeline_id, tenant_id, user_id, content, thread, source, created_at) \
                 VALUES ('pin-ghost', 'p-ghost', 'default', 'u1', 'hi', ?1, 'chat', ?2)",
                rusqlite::params![tid, now],
            )
            .unwrap();
            // 对照管道：同表各留一行（不应误删）
            conn.execute(
                "INSERT INTO pipeline_state (tenant_id, pipeline_id, field_key, field_value, updated_at) \
                 VALUES ('default', 'p-keep', 'task.status', 'running', ?1)",
                rusqlite::params![now],
            )
            .unwrap();
            conn.execute(
                "INSERT INTO pipeline_checkpoints (tenant_id, pipeline_id, checkpoint_id, step_no, state_json, created_at) \
                 VALUES ('default', 'p-keep', 'cp-keep', 0, '{}', ?1)",
                rusqlite::params![now],
            )
            .unwrap();
            conn.execute(
                "INSERT INTO pipeline_pending_inputs (id, pipeline_id, tenant_id, user_id, content, thread, source, created_at) \
                 VALUES ('pin-keep', 'p-keep', 'default', 'u1', 'hi', 'thread-other', 'chat', ?1)",
                rusqlite::params![now],
            )
            .unwrap();
        }

        store.delete_session(tid).await.unwrap();

        let conn = store.conn.lock();
        let count = |sql: &str| -> i64 { conn.query_row(sql, [], |r| r.get::<_, i64>(0)).unwrap() };
        assert_eq!(
            count("SELECT COUNT(*) FROM pipeline_state WHERE pipeline_id = 'p-ghost'"),
            0,
            "目标管道 state 应删除（任务面板幽灵行数据源）"
        );
        assert_eq!(
            count("SELECT COUNT(*) FROM pipeline_checkpoints WHERE pipeline_id = 'p-ghost'"),
            0,
            "目标管道 checkpoint 应删除（冷读兜底会重建幽灵任务）"
        );
        assert_eq!(
            count("SELECT COUNT(*) FROM pipeline_pending_inputs WHERE pipeline_id = 'p-ghost'"),
            0,
            "目标管道 pending 输入应删除（重启后被消费令任务复活）"
        );
        assert_eq!(
            count("SELECT COUNT(*) FROM pipeline_state WHERE pipeline_id = 'p-keep'"),
            1,
            "其他会话管道 state 不应误删"
        );
        assert_eq!(
            count("SELECT COUNT(*) FROM pipeline_checkpoints WHERE pipeline_id = 'p-keep'"),
            1,
            "其他会话管道 checkpoint 不应误删"
        );
        assert_eq!(
            count("SELECT COUNT(*) FROM pipeline_pending_inputs WHERE pipeline_id = 'p-keep'"),
            1,
            "其他会话管道 pending 输入不应误删"
        );
    }

    /// 损坏库 open 的双相契约（单测内串行验证，env 变量进程级共享不可并行）：
    ///
    /// 默认 fail-closed：先备份现场（.corrupt-*），再返回明确错误中止启动——
    /// 错误信息必须含备份路径与恢复指引，绝不静默重建空库继续跑；
    /// 设 AGENTOS_DB_AUTO_REBUILD=1：备份现场后重建空库继续，新库可正常读写。
    #[tokio::test]
    async fn test_open_corrupt_db_fail_closed_by_default_and_rebuild_optin() {
        // 模拟损坏的 SQLite 文件：文件头不是 "SQLite format 3"（进程崩溃/磁盘异常场景）
        let dir = tempfile::tempdir().unwrap();
        let db_path = dir.path().join("agentos_kernel.db");
        std::fs::write(&db_path, vec![0xDE_u8; 8192]).unwrap();

        // ── 相位一：默认 fail-closed ──
        std::env::remove_var("AGENTOS_DB_AUTO_REBUILD");
        let err = match SqliteStore::open(db_path.to_str().unwrap()) {
            Err(e) => e,
            Ok(_) => panic!("默认路径损坏库必须 fail-closed 拒绝打开"),
        };
        let msg = err.to_string();
        assert!(
            msg.contains(".corrupt-"),
            "错误信息必须含备份现场路径前缀: {msg}"
        );
        assert!(
            msg.contains("AGENTOS_DB_AUTO_REBUILD"),
            "错误信息必须含恢复指引（重建开关）: {msg}"
        );

        // 损坏文件应被备份保留现场（agentos_kernel.db.corrupt-*），且原路径已撤位
        let backup_names: Vec<String> = std::fs::read_dir(dir.path())
            .unwrap()
            .filter_map(|e| e.ok())
            .map(|e| e.file_name().to_string_lossy().to_string())
            .filter(|n| n.starts_with("agentos_kernel.db.corrupt-"))
            .collect();
        assert!(
            !backup_names.is_empty(),
            "损坏文件应被备份保留现场，实际: {:?}",
            backup_names
        );

        // ── 相位二：显式选择自动重建（AGENTOS_DB_AUTO_REBUILD=1）──
        std::env::set_var("AGENTOS_DB_AUTO_REBUILD", "1");
        let store = SqliteStore::open(db_path.to_str().unwrap()).unwrap();
        std::env::remove_var("AGENTOS_DB_AUTO_REBUILD");

        store
            .create_run("run_self_heal", "hash_abc", "default")
            .unwrap();
        let run = store.get_run("run_self_heal").await.unwrap();
        assert_eq!(run.run_id, "run_self_heal");
    }

    /// create_run 事务体契约：在调用方事务内 runs 与 branches main 两行同时可见，
    /// 回滚则两行俱灭——证明两行写在同一事务、不存在 autocommit 半态窗口。
    #[test]
    fn test_create_run_in_tx_writes_run_and_branch_atomically() {
        let conn = Connection::open_in_memory().unwrap();
        conn.execute_batch(DDL).unwrap();

        conn.execute_batch("BEGIN").unwrap();
        SqliteStore::create_run_in_tx(
            &conn,
            "run_tx",
            "hash_1",
            "tenant-1",
            "2026-09-11T00:00:00+00:00",
        )
        .unwrap();
        let counts: (i64, i64) = conn
            .query_row(
                "SELECT (SELECT COUNT(*) FROM runs), (SELECT COUNT(*) FROM branches)",
                [],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )
            .unwrap();
        assert_eq!(counts, (1, 1), "事务内两行必须同时可见");

        conn.execute_batch("ROLLBACK").unwrap();
        let counts_after: (i64, i64) = conn
            .query_row(
                "SELECT (SELECT COUNT(*) FROM runs), (SELECT COUNT(*) FROM branches)",
                [],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )
            .unwrap();
        assert_eq!(counts_after, (0, 0), "回滚必须两行俱灭（同一事务写入）");
    }

    /// 正常库不应被误备份重建（自愈只针对损坏场景，健康库原样打开）。
    #[tokio::test]
    async fn test_open_healthy_db_no_backup() {
        let dir = tempfile::tempdir().unwrap();
        let db_path = dir.path().join("healthy.db");
        let store = SqliteStore::open(db_path.to_str().unwrap()).unwrap();
        store
            .create_run("run_healthy", "hash_abc", "default")
            .unwrap();
        let run = store.get_run("run_healthy").await.unwrap();
        assert_eq!(run.run_id, "run_healthy");

        // 不应产生任何 .corrupt-* 备份文件
        let corrupt_files: Vec<String> = std::fs::read_dir(dir.path())
            .unwrap()
            .filter_map(|e| e.ok())
            .map(|e| e.file_name().to_string_lossy().to_string())
            .filter(|n| n.contains(".corrupt-"))
            .collect();
        assert!(
            corrupt_files.is_empty(),
            "健康库不应产生备份: {:?}",
            corrupt_files
        );
    }

    /// 任务删除语义：delete_pipeline 按 pipeline_id 级联清空
    /// runs/traces/branches/message_slots/pipeline_state/pipeline_checkpoints/
    /// pipeline_pending_inputs/pipeline_sessions，单事务；无记录时幂等返回 Ok。
    #[tokio::test]
    async fn test_delete_pipeline_cascades_all_data() {
        let store = SqliteStore::open_memory().unwrap();
        let pid = "pipeline-del-test";
        let now = chrono::Utc::now().to_rfc3339();

        {
            let conn = store.conn.lock();
            conn.execute(
                "INSERT INTO pipeline_sessions (pipeline_id, thread_id, tenant_id, created_at)                  VALUES (?1, ?2, 'default', ?3)",
                rusqlite::params![pid, "thread-del", now],
            )
            .unwrap();
            conn.execute(
                "INSERT INTO message_slots (tenant_id, pipeline_id, seq, message_id, run_id, created_at)                  VALUES ('default', ?1, 1, 'm-del-1', 'run-del-1', ?2)",
                rusqlite::params![pid, now],
            )
            .unwrap();
            conn.execute(
                "INSERT INTO runs (run_id, config_hash, status, tenant_id, created_at, current_branch)                  VALUES ('run-del-1', 'h', 'completed', 'default', ?1, 'main')",
                rusqlite::params![now],
            )
            .unwrap();
            conn.execute(
                "INSERT INTO traces (trace_id, run_id, branch_id, seq_in_branch, plugin_id, patch_type, patch_data, created_at)                  VALUES ('t-del-1', 'run-del-1', 'main', 0, 'p', 'state', '{}', ?1)",
                rusqlite::params![now],
            )
            .unwrap();
            conn.execute(
                "INSERT INTO branches (branch_id, run_id, parent_branch, parent_seq, tenant_id, created_at)                  VALUES ('main', 'run-del-1', NULL, NULL, 'default', ?1)",
                rusqlite::params![now],
            )
            .unwrap();
            conn.execute(
                "INSERT INTO pipeline_state (tenant_id, pipeline_id, field_key, field_value, updated_at)                  VALUES ('default', ?1, 'task.status', 'running', ?2)",
                rusqlite::params![pid, now],
            )
            .unwrap();
            conn.execute(
                "INSERT INTO pipeline_checkpoints (tenant_id, pipeline_id, checkpoint_id, step_no, state_json, created_at)                  VALUES ('default', ?1, 'cp-1', 0, '{}', ?2)",
                rusqlite::params![pid, now],
            )
            .unwrap();
            conn.execute(
                "INSERT INTO pipeline_pending_inputs (id, pipeline_id, tenant_id, user_id, content, thread, source, created_at)                  VALUES ('pin-del-1', ?1, 'default', 'u1', 'hi', 'thread-del', 'chat', ?2)",
                rusqlite::params![pid, now],
            )
            .unwrap();
        }

        store.delete_pipeline(pid).await.unwrap();

        let conn = store.conn.lock();
        let count = |sql: &str| -> i64 { conn.query_row(sql, [], |r| r.get::<_, i64>(0)).unwrap() };
        assert_eq!(
            count("SELECT COUNT(*) FROM pipeline_sessions"),
            0,
            "映射应删除"
        );
        assert_eq!(count("SELECT COUNT(*) FROM message_slots"), 0, "消息应删除");
        assert_eq!(count("SELECT COUNT(*) FROM runs"), 0, "runs 应删除");
        assert_eq!(count("SELECT COUNT(*) FROM traces"), 0, "traces 应删除");
        assert_eq!(count("SELECT COUNT(*) FROM branches"), 0, "branches 应删除");
        assert_eq!(
            count("SELECT COUNT(*) FROM pipeline_state"),
            0,
            "state 应删除"
        );
        assert_eq!(
            count("SELECT COUNT(*) FROM pipeline_checkpoints"),
            0,
            "checkpoints 应删除"
        );
        assert_eq!(
            count("SELECT COUNT(*) FROM pipeline_pending_inputs"),
            0,
            "pending 输入应删除"
        );
    }

    /// 幂等：删除不存在的管道返回 Ok，且不影响其它管道数据。
    #[tokio::test]
    async fn test_delete_pipeline_idempotent_keeps_others() {
        let store = SqliteStore::open_memory().unwrap();
        let now = chrono::Utc::now().to_rfc3339();

        {
            let conn = store.conn.lock();
            conn.execute(
                "INSERT INTO pipeline_state (tenant_id, pipeline_id, field_key, field_value, updated_at)                  VALUES ('default', 'other-pipeline', 'task.status', 'running', ?1)",
                rusqlite::params![now],
            )
            .unwrap();
        }

        store.delete_pipeline("no-such-pipeline").await.unwrap();

        let conn = store.conn.lock();
        let count: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM pipeline_state WHERE pipeline_id = 'other-pipeline'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(count, 1, "其它管道数据不应受影响");
    }

    #[tokio::test]
    async fn test_create_and_get_run() {
        let store = SqliteStore::open_memory().unwrap();
        store
            .create_run("run_1", "config_hash_1", "default")
            .unwrap();

        let run = store.get_run("run_1").await.unwrap();
        assert_eq!(run.run_id, "run_1");
        assert_eq!(run.config_hash, "config_hash_1");
        assert_eq!(run.tenant_id, "default");
        assert_eq!(run.status, RunStatus::Running);
        assert_eq!(run.current_branch, "main");
        assert_eq!(run.current_seq, 0);
    }

    /// 统一管道管理查询：runs × message_slots × pipeline_sessions × summaries 四表联结。
    ///
    /// 覆盖：真实执行管道（带消息槽+会话映射+汇总账本）可查出且字段齐全；
    /// 无消息槽的占位 run（旧引擎 start_run 产物）被过滤；status 过滤生效；
    /// 完成后 ended_at 可见。
    #[tokio::test]
    async fn test_list_pipelines_join() {
        let store = SqliteStore::open_memory().unwrap();
        store.create_run("run_1", "hash_1", "default").unwrap();
        store
            .link_pipeline_session("pipe_1", "thread_1", "default")
            .await
            .unwrap();
        store
            .apply_messages_ops_to_table(
                "pipe_1",
                "default",
                &[json!({
                    "op": "set",
                    "seq": 0,
                    "_run_id": "run_1",
                    "msg": {"role": "user", "content": "hi"},
                })],
            )
            .unwrap();

        // 占位 run：无消息槽 → 不应出现在管道快照
        store.create_run("run_orphan", "hash_2", "default").unwrap();

        let rows = store.list_pipelines_inner("default", None, 100).unwrap();
        assert_eq!(rows.len(), 1, "仅真实执行管道应出现，实际: {rows:?}");
        let r = &rows[0];
        assert_eq!(r.run_id, "run_1");
        assert_eq!(r.pipeline_id.as_deref(), Some("pipe_1"));
        assert_eq!(r.thread_id.as_deref(), Some("thread_1"));
        assert_eq!(r.status, RunStatus::Running);
        assert!(r.ended_at.is_none());

        // status 过滤：completed 尚无为空；完成后可查到且 ended_at 就位
        let completed = store
            .list_pipelines_inner("default", Some("completed"), 100)
            .unwrap();
        assert!(completed.is_empty());
        store
            .update_run_status("run_1", RunStatus::Completed, None, None)
            .await
            .unwrap();
        let completed = store
            .list_pipelines_inner("default", Some("completed"), 100)
            .unwrap();
        assert_eq!(completed.len(), 1);
        assert!(completed[0].ended_at.is_some());

        // 多租户隔离
        let other = store
            .list_pipelines_inner("other_tenant", None, 100)
            .unwrap();
        assert!(other.is_empty());
    }

    #[tokio::test]
    async fn test_get_run_not_found() {
        let store = SqliteStore::open_memory().unwrap();
        let result = store.get_run("nonexistent").await;
        assert!(result.is_err());
    }

    #[tokio::test]
    async fn test_update_run_status() {
        let store = SqliteStore::open_memory().unwrap();
        store.create_run("run_2", "hash", "default").unwrap();

        store
            .update_run_status("run_2", RunStatus::Suspended, None, None)
            .await
            .unwrap();
        let run = store.get_run("run_2").await.unwrap();
        assert_eq!(run.status, RunStatus::Suspended);

        store
            .update_run_status("run_2", RunStatus::Completed, None, None)
            .await
            .unwrap();
        let run = store.get_run("run_2").await.unwrap();
        assert_eq!(run.status, RunStatus::Completed);
        assert!(run.ended_at.is_some());
    }

    #[tokio::test]
    async fn test_update_run_status_with_branch() {
        let store = SqliteStore::open_memory().unwrap();
        store.create_run("run_3", "hash", "default").unwrap();

        store
            .update_run_status(
                "run_3",
                RunStatus::Running,
                Some("main.rollback.001"),
                Some(5),
            )
            .await
            .unwrap();

        let run = store.get_run("run_3").await.unwrap();
        assert_eq!(run.current_branch, "main.rollback.001");
        assert_eq!(run.current_seq, 5);
    }

    #[tokio::test]
    async fn test_store_and_get_blob() {
        let store = SqliteStore::open_memory().unwrap();
        let data = b"Hello, World!";
        let blob_id = store.store_blob(data, "text/plain").unwrap();

        // 元数据经 with_conn 直读 blobs 表验证
        let (mime_type, size_bytes): (String, i64) = store
            .with_conn(|c| {
                c.query_row(
                    "SELECT mime_type, size_bytes FROM blobs WHERE blob_id = ?1",
                    rusqlite::params![blob_id],
                    |r| Ok((r.get(0)?, r.get(1)?)),
                )
            })
            .unwrap();
        assert_eq!(mime_type, "text/plain");
        assert_eq!(size_bytes, data.len() as i64);

        let loaded = store.get_blob(&blob_id).await.unwrap();
        assert_eq!(loaded, data);
    }

    #[tokio::test]
    async fn test_blob_dedup() {
        let store = SqliteStore::open_memory().unwrap();
        let data = b"same content";
        let id1 = store.store_blob(data, "text/plain").unwrap();
        let id2 = store.store_blob(data, "text/plain").unwrap();
        assert_eq!(id1, id2); // 相同内容应得到相同 blob_id
    }

    /// 验证 get_messages_by_pipeline 按 pipeline_id 隔离 + 游标分页。
    /// 这是修复"按 thread_id 查询跨会话混杂"的核心。
    #[tokio::test]
    async fn test_get_messages_by_pipeline_isolation_and_cursor() {
        use agentos_core::traits::MessageQueryOpts;
        let store = SqliteStore::open_memory().unwrap();

        // 管道 A：2 条（slots 播种，零兼容：读路径只走 message_slots）
        store.create_run("rA", "h", "default").unwrap();
        store.apply_messages_ops_to_table("pipeA", "default", &[
            serde_json::json!({"op": "set", "seq": 0, "msg": {"role": "user", "content": "a-u"}}),
            serde_json::json!({"op": "set", "seq": 1, "msg": {"role": "assistant", "content": "a-ai"}}),
        ]).unwrap();
        // 管道 B：2 条（不同 pipeline_id）
        store.create_run("rB", "h", "default").unwrap();
        store.apply_messages_ops_to_table("pipeB", "default", &[
            serde_json::json!({"op": "set", "seq": 0, "msg": {"role": "user", "content": "b-u"}}),
            serde_json::json!({"op": "set", "seq": 1, "msg": {"role": "assistant", "content": "b-ai"}}),
        ]).unwrap();

        // 隔离：查 pipeA 只返 A 的 2 条，不含 B
        let msgs_a = store
            .get_messages_by_pipeline("pipeA", MessageQueryOpts::default())
            .await
            .unwrap();
        assert_eq!(msgs_a.len(), 2);
        assert_eq!(msgs_a[0].role, "user");
        assert_eq!(msgs_a[0].content_preview.as_deref(), Some("a-u"));
        assert!(msgs_a
            .iter()
            .all(|m| m.pipeline_id.as_deref() == Some("pipeA")));

        // 游标：after_sequence=0 应只返 seq>0 的（即 a2）
        let after = store
            .get_messages_by_pipeline(
                "pipeA",
                MessageQueryOpts {
                    after_sequence: Some(0),
                    ..Default::default()
                },
            )
            .await
            .unwrap();
        assert_eq!(after.len(), 1);
        assert_eq!(after[0].content_preview.as_deref(), Some("a-ai"));

        // limit：限制 1 条
        let limited = store
            .get_messages_by_pipeline(
                "pipeA",
                MessageQueryOpts {
                    limit: Some(1),
                    ..Default::default()
                },
            )
            .await
            .unwrap();
        assert_eq!(limited.len(), 1);
    }

    /// 验证 session CRUD + pipeline_ids JSON 持久化 + upsert 更新。
    /// 会话是聚合管道引用的标签夹。
    #[tokio::test]
    async fn test_session_crud_and_pipeline_ids() {
        use agentos_core::traits::SessionListFilter;
        use agentos_core::types::SessionRecord;
        let store = SqliteStore::open_memory().unwrap();
        let now = chrono::Utc::now().to_rfc3339();

        // 创建会话：主管道 pid_main
        let s1 = SessionRecord {
            thread_id: "thread_1".to_string(),
            title: Some("会话一".to_string()),
            intent: Some("测试".to_string()),
            current_state: "active".to_string(),
            agent_id: Some("agentos".to_string()),
            active_pipeline_id: Some("pid_main".to_string()),
            pipeline_ids: vec!["pid_main".to_string()],
            metadata: Some(json!({ "session_type": "main_pipeline", "pinned": true })),
            created_at: now.clone(),
            updated_at: now.clone(),
            last_active_at: Some(now.clone()),
        };
        store.create_session(&s1).await.unwrap();

        // 读取：pipeline_ids JSON 正确反序列化
        let got = store.get_session("thread_1").await.unwrap().unwrap();
        assert_eq!(got.title.as_deref(), Some("会话一"));
        assert_eq!(got.pipeline_ids, vec!["pid_main".to_string()]);
        assert_eq!(got.active_pipeline_id.as_deref(), Some("pid_main"));
        assert_eq!(got.metadata.as_ref().unwrap()["pinned"], true);

        // upsert 更新：追加子管道 pid_sub，但 active 不动（不翻转既有 active 标记）
        let mut s1b = got.clone();
        s1b.pipeline_ids.push("pid_sub".to_string());
        s1b.updated_at = chrono::Utc::now().to_rfc3339();
        store.update_session(&s1b).await.unwrap();
        let got2 = store.get_session("thread_1").await.unwrap().unwrap();
        assert_eq!(got2.pipeline_ids, vec!["pid_main", "pid_sub"]);
        assert_eq!(
            got2.active_pipeline_id.as_deref(),
            Some("pid_main"),
            "子管道注册不应覆盖 active"
        );

        // 创建第二个会话（不同 session_type）
        let s2 = SessionRecord {
            thread_id: "thread_2".to_string(),
            title: None,
            intent: None,
            current_state: "active".to_string(),
            agent_id: None,
            active_pipeline_id: Some("pid2".to_string()),
            pipeline_ids: vec!["pid2".to_string()],
            metadata: Some(json!({ "session_type": "cli" })),
            created_at: now.clone(),
            updated_at: now.clone(),
            last_active_at: None,
        };
        store.create_session(&s2).await.unwrap();

        // list：按 session_type 过滤
        let main_only = store
            .list_sessions(SessionListFilter {
                session_type: Some("main_pipeline".to_string()),
                limit: Some(100),
            })
            .await
            .unwrap();
        assert_eq!(main_only.len(), 1);
        assert_eq!(main_only[0].thread_id, "thread_1");

        // list 全部（不过滤 session_type）
        let all = store
            .list_sessions(SessionListFilter::default())
            .await
            .unwrap();
        assert_eq!(all.len(), 2);

        // get 不存在的会话
        assert!(store.get_session("nonexistent").await.unwrap().is_none());

        // 删除会话：仅删标签夹行，幂等（删不存在的也 Ok）
        store.delete_session("thread_1").await.unwrap();
        assert!(
            store.get_session("thread_1").await.unwrap().is_none(),
            "删除后应查无记录"
        );
        assert_eq!(
            store
                .list_sessions(SessionListFilter::default())
                .await
                .unwrap()
                .len(),
            1,
            "只删了 thread_1，thread_2 应保留"
        );
        // 幂等：再删一次不报错
        store.delete_session("thread_1").await.unwrap();
    }

    /// 验证删除会话级联：主管道 + 子任务管道的 messages/traces/runs 全清，
    /// 映射表同步清理，且不误删其他会话数据。
    #[tokio::test]
    async fn test_delete_session_cascade_includes_sub_pipelines() {
        use agentos_core::types::SessionRecord;
        let store = SqliteStore::open_memory().unwrap();
        let now = chrono::Utc::now().to_rfc3339();

        // 会话 thread_1：主管道 pid_main + 子管道 pid_sub（不同 pipeline_id，同 thread_id）
        store
            .link_pipeline_session("pid_main", "thread_1", "default")
            .await
            .unwrap();
        store
            .link_pipeline_session("pid_sub", "thread_1", "default")
            .await
            .unwrap();
        let s1 = SessionRecord {
            thread_id: "thread_1".to_string(),
            title: None,
            intent: None,
            current_state: "active".to_string(),
            agent_id: None,
            active_pipeline_id: Some("pid_main".to_string()),
            pipeline_ids: vec!["pid_main".to_string()],
            metadata: None,
            created_at: now.clone(),
            updated_at: now.clone(),
            last_active_at: None,
        };
        store.create_session(&s1).await.unwrap();

        // 主管道数据：1 run + 2 messages + 1 trace + 1 execution_record
        store.create_run("run_main", "h", "default").unwrap();
        store.apply_messages_ops_to_table("pid_main", "default", &[
            serde_json::json!({"op": "set", "seq": 0, "msg": {"role": "user", "content": "u"}, "_run_id": "run_main"}),
            serde_json::json!({"op": "set", "seq": 1, "msg": {"role": "assistant", "content": "a"}, "_run_id": "run_main"}),
        ]).unwrap();
        store
            .append_trace(TraceEntry {
                trace_id: "t1".into(),
                run_id: "run_main".into(),
                branch_id: "main".into(),
                seq_in_branch: 0,
                plugin_id: "prepare".into(),
                patch_type: PatchType::StateUpdate,
                patch_data: json!({"k": "v"}),
                created_at: now.clone(),
            })
            .await
            .unwrap();

        // 子管道数据：1 run + 1 message + 1 trace（独立 pipeline_id pid_sub）
        store.create_run("run_sub", "h", "default").unwrap();
        store.apply_messages_ops_to_table("pid_sub", "default", &[
            serde_json::json!({"op": "set", "seq": 0, "msg": {"role": "user", "content": "su"}, "_run_id": "run_sub"}),
        ]).unwrap();
        store
            .append_trace(TraceEntry {
                trace_id: "t2".into(),
                run_id: "run_sub".into(),
                branch_id: "main".into(),
                seq_in_branch: 0,
                plugin_id: "core".into(),
                patch_type: PatchType::StateUpdate,
                patch_data: json!({"k2": "v2"}),
                created_at: now.clone(),
            })
            .await
            .unwrap();

        // 另一会话 thread_2：数据应保留
        store
            .link_pipeline_session("pid_other", "thread_2", "default")
            .await
            .unwrap();
        let s2 = SessionRecord {
            thread_id: "thread_2".to_string(),
            title: None,
            intent: None,
            current_state: "active".to_string(),
            agent_id: None,
            active_pipeline_id: Some("pid_other".to_string()),
            pipeline_ids: vec!["pid_other".to_string()],
            metadata: None,
            created_at: now.clone(),
            updated_at: now.clone(),
            last_active_at: None,
        };
        store.create_session(&s2).await.unwrap();
        store.create_run("run_other", "h", "default").unwrap();
        store.apply_messages_ops_to_table("pid_other", "default", &[
            serde_json::json!({"op": "set", "seq": 0, "msg": {"role": "user", "content": "ou"}, "_run_id": "run_other"}),
        ]).unwrap();

        // 删 thread_1：应级联清掉主管道 + 子管道全部数据
        store.delete_session("thread_1").await.unwrap();

        // thread_1 的数据应全部归零
        let pids = store
            .list_pipeline_ids_by_thread("thread_1", "default")
            .await
            .unwrap();
        assert!(pids.is_empty(), "映射表应已清理，无残留 pipeline_id");
        assert!(
            store.get_session("thread_1").await.unwrap().is_none(),
            "sessions 行应删除"
        );
        let main_msgs = store
            .get_messages_by_pipeline(
                "pid_main",
                agentos_core::traits::MessageQueryOpts::default(),
            )
            .await
            .unwrap();
        assert!(main_msgs.is_empty(), "主管道 messages 应清空");
        let sub_msgs = store
            .get_messages_by_pipeline("pid_sub", agentos_core::traits::MessageQueryOpts::default())
            .await
            .unwrap();
        assert!(sub_msgs.is_empty(), "子管道 messages 应清空");
        // 轨迹残留经 with_conn 直读 traces 表验证
        let residual: Vec<String> = store
            .with_conn(|c| {
                let mut stmt =
                    c.prepare("SELECT run_id FROM traces WHERE run_id IN ('run_main', 'run_sub')")?;
                let rows = stmt.query_map([], |r| r.get::<_, String>(0))?;
                rows.collect()
            })
            .unwrap();
        assert!(residual.is_empty(), "traces 应清空");

        // thread_2 数据应保留
        assert!(
            store.get_session("thread_2").await.unwrap().is_some(),
            "thread_2 应保留"
        );
        let other_msgs = store
            .get_messages_by_pipeline(
                "pid_other",
                agentos_core::traits::MessageQueryOpts::default(),
            )
            .await
            .unwrap();
        assert_eq!(other_msgs.len(), 1, "thread_2 的 messages 不应被误删");

        // 幂等：再删一次不报错
        store.delete_session("thread_1").await.unwrap();
    }

    /// 按管道唯一坐标反查所属会话 thread_id（chat.send_message 注入分支坐标解析）。
    /// 只查 pipeline_sessions：命中返回真实 thread、未命中 None（不做 sessions 回退）。
    #[tokio::test]
    async fn test_get_thread_id_by_pipeline() {
        let store = SqliteStore::open_memory().unwrap();
        store
            .link_pipeline_session("12hex_main", "thread-abc", "default")
            .await
            .unwrap();
        store
            .link_pipeline_session("12hex_sub", "thread-abc", "default")
            .await
            .unwrap();

        // 命中：主管道 / 子任务管道均解析回所属会话 thread
        assert_eq!(
            store.get_thread_id_by_pipeline("12hex_main").await.unwrap(),
            Some("thread-abc".to_string()),
            "主管道应解析出所属 thread"
        );
        assert_eq!(
            store.get_thread_id_by_pipeline("12hex_sub").await.unwrap(),
            Some("thread-abc".to_string()),
            "子任务管道应解析出所属 thread"
        );

        // 未命中：孤儿/伪造 id → None（调用方据此报协议错误），无 sessions 回退
        assert_eq!(
            store.get_thread_id_by_pipeline("orphan").await.unwrap(),
            None,
            "无映射的 pipeline_id 应返回 None"
        );

        // 边界：空串同样 None（不 panic、不误报）
        assert_eq!(
            store.get_thread_id_by_pipeline("").await.unwrap(),
            None,
            "空 pipeline_id 应返回 None"
        );
    }

    #[tokio::test]
    async fn test_append_trace_reads_back_in_seq_order() {
        let store = SqliteStore::open_memory().unwrap();
        store.create_run("run_5", "hash", "default").unwrap();

        // 追加 3 条 trace
        for i in 0..3u32 {
            let entry = TraceEntry {
                trace_id: format!("trace_{}", i),
                run_id: "run_5".to_string(),
                branch_id: "main".to_string(),
                seq_in_branch: i,
                plugin_id: format!("plugin_{}", i),
                patch_type: PatchType::StateUpdate,
                patch_data: json!({"key": format!("value_{}", i)}),
                created_at: chrono::Utc::now().to_rfc3339(),
            };
            store.append_trace(entry).await.unwrap();
        }

        // 落库行经 with_conn 直读验证（按 seq 升序）
        let rows: Vec<(String, String)> = store
            .with_conn(|c| {
                let mut stmt = c.prepare(
                    "SELECT plugin_id, patch_data FROM traces WHERE branch_id = 'main' \
                     AND seq_in_branch >= 0 AND seq_in_branch <= 2 ORDER BY seq_in_branch ASC",
                )?;
                let rows =
                    stmt.query_map([], |r| Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?)))?;
                rows.collect()
            })
            .unwrap();
        assert_eq!(rows.len(), 3);
        assert_eq!(rows[0].0, "plugin_0");
        assert_eq!(rows[2].0, "plugin_2");
        let patch_1: serde_json::Value = serde_json::from_str(&rows[1].1).unwrap();
        assert_eq!(patch_1["key"], "value_1");
    }

    /// P0-5 关键验收：跨租户隔离。
    ///
    /// 租户 A 写入数据后，切换到租户 B 的 task_local 作用域：
    /// - get_run / get_messages 必须读不到 A 的数据（隔离生效）
    /// - 切回 A 作用域后仍可读到（数据未丢失）
    ///
    /// tenant_id 通过 task_local 隐式传递，StorageBackend trait 签名不变。
    #[tokio::test]
    async fn test_cross_tenant_isolation() {
        let store = SqliteStore::open_memory().unwrap();

        // 租户 A：创建 run + 追加消息
        let ctx_a = TenantContext::new("tenant_a", "session_a");
        agentos_tenant::scope(ctx_a, async {
            store.create_run("run_a", "hash_a", "tenant_a").unwrap();
            store
                .apply_messages_ops_to_table("pid_a", "tenant_a", &[
                    serde_json::json!({"op": "set", "seq": 0, "msg": {"role": "user", "content": "hi-a"}, "_run_id": "run_a"}),
                ])
                .unwrap();

            // 自身作用域内可读到
            let run = store.get_run("run_a").await.unwrap();
            assert_eq!(run.tenant_id, "tenant_a");
            let msgs = store.get_slot_messages_by_pipeline("pid_a", "tenant_a", agentos_core::traits::MessageQueryOpts::default()).unwrap();
            assert_eq!(msgs.len(), 1);
        })
        .await;

        // 切换到租户 B 的作用域：必须读不到 A 的数据
        let ctx_b = TenantContext::new("tenant_b", "session_b");
        agentos_tenant::scope(ctx_b, async {
            let run_result = store.get_run("run_a").await;
            assert!(
                run_result.is_err(),
                "tenant B must not see tenant A's run (isolation)"
            );
            let msgs = store
                .get_slot_messages_by_pipeline(
                    "pid_a",
                    "tenant_b",
                    agentos_core::traits::MessageQueryOpts::default(),
                )
                .unwrap();
            assert!(
                msgs.is_empty(),
                "tenant B must not see tenant A's messages (isolation)"
            );
        })
        .await;

        // 切回 A：数据仍在（未丢失）
        agentos_tenant::scope(TenantContext::new("tenant_a", "session_a"), async {
            let run = store.get_run("run_a").await.unwrap();
            assert_eq!(run.tenant_id, "tenant_a");
        })
        .await;
    }

    #[tokio::test]
    async fn test_session_cross_tenant_isolation() {
        use agentos_core::traits::SessionListFilter;
        let store = SqliteStore::open_memory().unwrap();
        let now = chrono::Utc::now().to_rfc3339();

        let mk = |tid: &str| SessionRecord {
            thread_id: tid.to_string(),
            title: Some("t".to_string()),
            intent: None,
            current_state: "active".to_string(),
            agent_id: None,
            active_pipeline_id: None,
            pipeline_ids: vec![],
            metadata: None,
            created_at: now.clone(),
            updated_at: now.clone(),
            last_active_at: None,
        };

        // 租户 A：建会话 thread_a
        agentos_tenant::scope(TenantContext::new("tenant_a", "s_a"), async {
            store.create_session(&mk("thread_a")).await.unwrap();
            assert!(store.get_session("thread_a").await.unwrap().is_some());
            assert_eq!(
                store
                    .list_sessions(SessionListFilter::default())
                    .await
                    .unwrap()
                    .len(),
                1
            );
        })
        .await;

        // 租户 B：读不到 A 的会话；list 为空；get 返回 None；delete 不影响 A
        agentos_tenant::scope(TenantContext::new("tenant_b", "s_b"), async {
            assert!(
                store.get_session("thread_a").await.unwrap().is_none(),
                "tenant B must not see tenant A's session"
            );
            assert!(
                store
                    .list_sessions(SessionListFilter::default())
                    .await
                    .unwrap()
                    .is_empty(),
                "tenant B must not list tenant A's sessions"
            );
            // B 尝试删 A 的会话：tenant 过滤下不应影响 A
            store.delete_session("thread_a").await.unwrap();
        })
        .await;

        // 切回 A：会话仍在（B 的删除被 tenant 过滤挡住）
        agentos_tenant::scope(TenantContext::new("tenant_a", "s_a"), async {
            assert!(
                store.get_session("thread_a").await.unwrap().is_some(),
                "tenant B's delete must not affect tenant A (isolation)"
            );
            // A 自己删自己的，成功
            store.delete_session("thread_a").await.unwrap();
            assert!(store.get_session("thread_a").await.unwrap().is_none());
        })
        .await;
    }

    // ── 分层持久化投影测试 ──────────────────────────────────────

    /// upsert_state_field 幂等 + load_pipeline_state 往返。
    #[tokio::test]
    async fn test_upsert_and_load_pipeline_state() {
        let store = SqliteStore::open_memory().unwrap();
        let pid = "pipe_state_test";
        // 首次 upsert
        store
            .upsert_state_field(pid, "default", "track.total_tokens", &json!(150))
            .unwrap();
        // 再次 upsert 覆盖（累计语义）
        store
            .upsert_state_field(pid, "default", "track.total_tokens", &json!(300))
            .unwrap();
        store
            .upsert_state_field(pid, "default", "track.llm_usage", &json!({"prompt": 10}))
            .unwrap();

        let loaded = store.load_pipeline_state(pid, "default").unwrap();
        assert_eq!(
            loaded.get("track.total_tokens"),
            Some(&json!(300)),
            "应取最新覆盖值"
        );
        assert_eq!(loaded.get("track.llm_usage"), Some(&json!({"prompt": 10})));
        assert_eq!(loaded.len(), 2, "应有 2 个字段");
    }

    // ── 域11：pending 输入队列（ADR-2026-08-26）──────────────────────

    /// 构造一条 pending 输入（测试辅助）。
    fn pending_input(id: &str, pid: &str, content: &str, created: &str) -> PendingInputRecord {
        PendingInputRecord {
            id: id.to_string(),
            pipeline_id: pid.to_string(),
            tenant_id: "default".to_string(),
            user_id: "u1".to_string(),
            content: content.to_string(),
            thread: format!("thread-{pid}"),
            source: PendingInputSource::User,
            agent_id: "agentos".to_string(),
            route_id: pid.to_string(),
            thinking_strength: String::new(),
            client_message_id: String::new(),
            execution_context: None,
            state_overlay: Some(json!({"task.goal": content})),
            created_at: created.to_string(),
        }
    }

    /// 入队/列出/弹出往返 + FIFO 序（created_at, id 升序）。
    #[tokio::test]
    async fn test_pending_inputs_enqueue_list_pop_fifo() {
        let store = SqliteStore::open_memory().unwrap();
        let pid = "pipe_pending_1";
        // 逆序入队（created_at 升序时队首应为 p1）
        store
            .enqueue_pending_input(
                "default",
                pid,
                &pending_input("in_3", pid, "第三条", "2026-08-26T03:00:00Z"),
            )
            .unwrap();
        store
            .enqueue_pending_input(
                "default",
                pid,
                &pending_input("in_1", pid, "第一条", "2026-08-26T01:00:00Z"),
            )
            .unwrap();
        store
            .enqueue_pending_input(
                "default",
                pid,
                &pending_input("in_2", pid, "第二条", "2026-08-26T02:00:00Z"),
            )
            .unwrap();

        let listed = store.list_pending_inputs("default", pid).unwrap();
        let ids: Vec<&str> = listed.iter().map(|i| i.id.as_str()).collect();
        assert_eq!(
            ids,
            vec!["in_1", "in_2", "in_3"],
            "FIFO 序 = created_at 升序"
        );
        assert_eq!(listed[0].content, "第一条");
        assert_eq!(listed[0].source, PendingInputSource::User);
        assert_eq!(
            listed[0].state_overlay,
            Some(json!({"task.goal": "第一条"})),
            "overlay 往返保真"
        );

        // pop 取队首并删除（消费瞬态）
        let popped = store.pop_pending_input("default", pid).unwrap();
        assert_eq!(popped.unwrap().id, "in_1", "pop 取 FIFO 队首");
        let remaining = store.list_pending_inputs("default", pid).unwrap();
        assert_eq!(
            remaining.iter().map(|i| i.id.as_str()).collect::<Vec<_>>(),
            vec!["in_2", "in_3"],
            "pop 后剩余两条"
        );
    }

    /// 空队列 pop 返回 None；不同租户/管道隔离。
    #[tokio::test]
    async fn test_pending_inputs_empty_and_isolation() {
        let store = SqliteStore::open_memory().unwrap();
        assert!(
            store
                .pop_pending_input("default", "pipe_empty")
                .unwrap()
                .is_none(),
            "空队列 pop 返回 None"
        );
        store
            .enqueue_pending_input(
                "default",
                "pipe_a",
                &pending_input("a1", "pipe_a", "A", "2026-08-26T01:00:00Z"),
            )
            .unwrap();
        assert!(
            store
                .pop_pending_input("other_tenant", "pipe_a")
                .unwrap()
                .is_none(),
            "跨租户不可见"
        );
        assert!(
            store
                .pop_pending_input("default", "pipe_b")
                .unwrap()
                .is_none(),
            "跨管道不可见"
        );
    }

    /// 同 id 重复入队幂等（INSERT OR IGNORE）。
    #[tokio::test]
    async fn test_pending_inputs_enqueue_idempotent() {
        let store = SqliteStore::open_memory().unwrap();
        let pid = "pipe_pending_idem";
        let rec = pending_input("dup1", pid, "原始", "2026-08-26T01:00:00Z");
        store.enqueue_pending_input("default", pid, &rec).unwrap();
        store.enqueue_pending_input("default", pid, &rec).unwrap();
        let listed = store.list_pending_inputs("default", pid).unwrap();
        assert_eq!(listed.len(), 1, "同 id 重复入队不产生重复条目");
    }

    /// update/delete/clear 语义：存在→生效；不存在→Ok(false)/Ok(0)。
    #[tokio::test]
    async fn test_pending_inputs_update_delete_clear() {
        let store = SqliteStore::open_memory().unwrap();
        let pid = "pipe_pending_mut";
        store
            .enqueue_pending_input(
                "default",
                pid,
                &pending_input("m1", pid, "旧内容", "2026-08-26T01:00:00Z"),
            )
            .unwrap();
        store
            .enqueue_pending_input(
                "default",
                pid,
                &pending_input("m2", pid, "另一条", "2026-08-26T02:00:00Z"),
            )
            .unwrap();

        // 修改
        assert!(
            store
                .update_pending_input_content("default", pid, "m1", "新内容")
                .unwrap(),
            "存在的条目更新返回 true"
        );
        let listed = store.list_pending_inputs("default", pid).unwrap();
        assert_eq!(listed[0].content, "新内容", "update 覆盖 content");
        assert_eq!(listed[0].id, "m1", "update 不改变 FIFO 位置");
        assert!(
            !store
                .update_pending_input_content("default", pid, "ghost", "x")
                .unwrap(),
            "不存在条目 update 返回 false"
        );

        // 删除
        assert!(
            store.delete_pending_input("default", pid, "m2").unwrap(),
            "删除存在条目返回 true"
        );
        assert!(
            !store.delete_pending_input("default", pid, "m2").unwrap(),
            "删除不存在条目返回 false"
        );

        // 清空
        assert_eq!(
            store.clear_pending_inputs("default", pid).unwrap(),
            1,
            "清空返回删除条数"
        );
        assert!(
            store
                .list_pending_inputs("default", pid)
                .unwrap()
                .is_empty(),
            "清空后队列空"
        );
    }

    /// save_checkpoint / load_latest_checkpoint 往返 + 取最新。
    #[tokio::test]
    async fn test_save_and_load_checkpoint() {
        let store = SqliteStore::open_memory().unwrap();
        let pid = "pipe_ckpt_test";
        store
            .save_checkpoint(pid, "default", 5, &json!({"messages": [], "step": 5}))
            .unwrap();
        store
            .save_checkpoint(
                pid,
                "default",
                10,
                &json!({"messages": [{"role":"user","content":"hi"}], "step": 10}),
            )
            .unwrap();

        let latest = store.load_latest_checkpoint(pid, "default").unwrap();
        assert!(latest.is_some());
        let (step_no, state) = latest.unwrap();
        assert_eq!(step_no, 10, "应取 step_no 最大的 checkpoint");
        assert_eq!(state["step"], json!(10));
    }

    #[tokio::test]
    async fn checkpoint_strips_control_state_keys_including_should_stop() {
        // 控制状态键契约：should_stop/ended/suspended/router.stop_reason 同属
        // per-run 控制键（署名与终止请求同写同消），checkpoint 瘦身剥离——
        // 冷恢复不得复活上一 run 的终止请求与署名。本测试裸 store（未注入
        // 声明键）只验内核自有键；插件语义 per-run 键
        // （conversation_mode/core_type/core_plugin 等）由 manifest
        // state.volatile_keys 声明、经 set_declared_volatile_keys 注入后与
        // 内核自有键在同一剥离点剥离
        // （见 pipeline_loop::tests::run_end_checkpoint_strips_declared_volatile_keys）。
        let store = SqliteStore::open_memory().unwrap();
        store
            .save_checkpoint(
                "pipe_ctrl",
                "default",
                3,
                &json!({
                    "task.status": "running",
                    "should_stop": true,
                    "ended": true,
                    "suspended": true,
                    "router.stop_reason": "budget_exhausted"
                }),
            )
            .unwrap();
        let (_, state) = store
            .load_latest_checkpoint("pipe_ctrl", "default")
            .unwrap()
            .unwrap();
        for key in ["should_stop", "ended", "suspended", "router.stop_reason"] {
            assert!(state.get(key).is_none(), "{key} 不得残留进 checkpoint");
        }
        assert_eq!(state["task.status"], json!("running"), "持久键不受瘦身影响");
    }

    // ── A7：槽位 blob 解码的「合法缺失 vs 损坏」区分 ──

    #[test]
    fn decode_slot_message_distinguishes_missing_from_corruption() {
        // blob_id NULL：合法缺失（无指针）→ 空对象，不算损坏
        let (v, reason) = decode_slot_message(None, None);
        assert!(v.as_object().is_some_and(|o| o.is_empty()));
        assert!(reason.is_none(), "NULL blob_id 不得记为损坏");

        // 有 blob_id 但 blob 行缺失 → 损坏（降级 + 原因）
        let (v, reason) = decode_slot_message(Some("b_missing"), None);
        assert!(v.as_object().is_some_and(|o| o.is_empty()));
        assert!(reason.unwrap().contains("blob 行缺失"));

        // 非 UTF-8 → 损坏
        let (_, reason) = decode_slot_message(Some("b_utf8"), Some(&[0xff, 0xfe]));
        assert!(reason.unwrap().contains("UTF-8"));

        // JSON 解析失败 → 损坏
        let (_, reason) = decode_slot_message(Some("b_badjson"), Some(b"{not json"));
        assert!(reason.unwrap().contains("JSON"));

        // 正常 JSON → 原样返回，无损坏
        let (v, reason) = decode_slot_message(Some("b_ok"), Some(br#"{"role":"user"}"#));
        assert_eq!(v["role"], json!("user"));
        assert!(reason.is_none());
    }

    // ── DedicatedDbPool：固定长命 DB 线程池行为契约 ──

    /// 并发 50 任务全部完成且结果正确：池不丢任务、不串结果。
    /// 载荷两组有区分度（短文本 / 4KB+ 长文本），性质断言 = 50 个互异内容
    /// 恰好得到 50 个互异 blob_id 且逐一回读一致。
    #[tokio::test]
    async fn db_pool_concurrent_50_tasks_all_complete_and_correct() {
        let store = Arc::new(SqliteStore::open_memory().unwrap());
        let mut handles = Vec::new();
        for i in 0..50u32 {
            let store = Arc::clone(&store);
            handles.push(tokio::spawn(async move {
                let (data, mime) = if i % 2 == 0 {
                    (format!("payload-{i}").into_bytes(), "text/plain")
                } else {
                    (vec![b'x'; 4096 + i as usize], "application/octet-stream")
                };
                // 全限定调用：固有同名同步 store_blob 会遮蔽 trait 的 async 版本
                let blob_id =
                    <SqliteStore as StorageBackend>::store_blob(&store, &data, mime).await?;
                Ok::<_, StorageError>((blob_id, data))
            }));
        }
        let mut seen = std::collections::HashSet::new();
        for h in handles {
            let (blob_id, data) = h.await.unwrap().unwrap();
            let roundtrip = store.get_blob(&blob_id).await.unwrap();
            assert_eq!(roundtrip, data, "回读内容必须与写入一致");
            assert!(seen.insert(blob_id), "blob_id 不得重复（任务串结果）");
        }
        assert_eq!(seen.len(), 50, "50 个互异内容必须全部落库，无任务丢失");
    }

    /// 关停语义：池 Drop（发送端关闭）后，已入队任务必须先排空再让线程退出
    /// ——关停不丢任务（8 任务 > 4 线程，必然存在入队未执行的任务）。
    #[test]
    fn db_pool_shutdown_drains_queued_jobs_without_loss() {
        let pool = DedicatedDbPool::spawn().unwrap();
        let (done_tx, done_rx) = std::sync::mpsc::channel::<usize>();
        for i in 0..8usize {
            let done_tx = done_tx.clone();
            pool.dispatch(Box::new(move || {
                let _ = done_tx.send(i);
            }))
            .unwrap();
        }
        drop(done_tx);
        drop(pool);
        // iter() 在通道关闭且排空后结束：每个任务闭包持有 done_tx 的一个 clone，
        // 全部执行完（clone 随闭包释放）iter 才收尾——collect 完成即"全部执行完"。
        let mut got: Vec<usize> = done_rx.iter().collect();
        got.sort_unstable();
        assert_eq!(got, (0..8).collect::<Vec<_>>(), "关停不得丢任务");
    }

    /// 关停后不再接受：通道断连（池 Drop 后发送端消亡的等价状态）时 dispatch
    /// 返回错误而非静默入队丢失。
    #[test]
    fn db_pool_dispatch_rejected_after_shutdown() {
        let (tx, rx) = std::sync::mpsc::sync_channel::<DbJob>(1);
        drop(rx);
        let pool = DedicatedDbPool {
            tx,
            blocked_dispatches: std::sync::atomic::AtomicU64::new(0),
        };
        let err = pool.dispatch(Box::new(|| {})).unwrap_err();
        assert!(matches!(err, StorageError::Database(_)));
        assert!(
            err.to_string().contains("shut down"),
            "拒绝信息须可诊断: {err}"
        );
    }

    /// 背压契约：队列打满后 dispatch 必须阻塞等待（不再无界积压），消费
    /// 恢复后阻塞的 dispatch 返回、任务全部执行。单 worker + 容量 1 注入，
    /// gate 门控 worker 占位，使"队列满"成为确定性状态（非 sleep 竞速）。
    #[test]
    fn db_pool_dispatch_backpressures_when_full_then_recovers() {
        let pool = std::sync::Arc::new(DedicatedDbPool::spawn_with(1, 1).unwrap());
        let executed = std::sync::Arc::new(std::sync::atomic::AtomicUsize::new(0));

        // job1：占住唯一 worker，等 gate 放行——期间队列无法被消费。
        let (gate_tx, gate_rx) = std::sync::mpsc::channel::<()>();
        let gate_rx = std::sync::Arc::new(parking_lot::Mutex::new(gate_rx));
        let (started_tx, started_rx) = std::sync::mpsc::channel::<()>();
        let executed1 = std::sync::Arc::clone(&executed);
        pool.dispatch(Box::new(move || {
            let _ = started_tx.send(());
            let _ = gate_rx.lock().recv();
            executed1.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
        }))
        .unwrap();
        started_rx
            .recv()
            .expect("worker 应开始执行 job1 并停在 gate");

        // job2：占满唯一队列槽位。
        let executed2 = std::sync::Arc::clone(&executed);
        pool.dispatch(Box::new(move || {
            executed2.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
        }))
        .unwrap();

        // job3：队列满 → dispatch 必须阻塞，在独立线程派发。
        let pool3 = std::sync::Arc::clone(&pool);
        let executed3 = std::sync::Arc::clone(&executed);
        let dispatcher = std::thread::spawn(move || {
            pool3
                .dispatch(Box::new(move || {
                    executed3.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
                }))
                .expect("恢复消费后阻塞的 dispatch 必须成功入队");
        });

        std::thread::sleep(std::time::Duration::from_millis(100));
        assert!(
            !dispatcher.is_finished(),
            "队列满时 dispatch 必须阻塞（背压），不得立即返回"
        );
        assert_eq!(
            executed.load(std::sync::atomic::Ordering::SeqCst),
            0,
            "背压未解除前不得有任何任务执行"
        );

        // 放行 job1 → worker 依次消费 job2、job3，阻塞的 dispatch 返回。
        drop(gate_tx);
        dispatcher.join().unwrap();
        let deadline = std::time::Instant::now() + std::time::Duration::from_secs(5);
        while executed.load(std::sync::atomic::Ordering::SeqCst) < 3 {
            assert!(
                std::time::Instant::now() < deadline,
                "放行后任务须全部执行完（恢复消费），实际: {}",
                executed.load(std::sync::atomic::Ordering::SeqCst)
            );
            std::thread::sleep(std::time::Duration::from_millis(10));
        }
        assert_eq!(
            pool.blocked_dispatches
                .load(std::sync::atomic::Ordering::SeqCst),
            1,
            "一次满载派发必须累计一次背压计数（可观测不静默）"
        );
    }

    /// 任务 panic 必须映射为错误（不悬挂调用方），且长命线程不被打死——
    /// 后续任务仍正常完成。
    #[tokio::test]
    async fn db_pool_task_panic_maps_to_error_and_pool_survives() {
        let store = SqliteStore::open_memory().unwrap();
        let r: Result<(), StorageError> = store
            .blocking(|_this| -> Result<(), StorageError> { panic!("boom") })
            .await;
        assert!(
            matches!(r, Err(StorageError::Database(_))),
            "panic 必须映射为 StorageError，实际: {r:?}"
        );
        let ok: Result<u32, StorageError> = store.blocking(|_this| Ok(7)).await;
        assert_eq!(ok.unwrap(), 7, "panic 后池必须仍然可用");
    }

    // ── B6：pipeline-state 批量事务 upsert（all-or-nothing）─────────────

    #[test]
    fn upsert_state_fields_batch_is_all_or_nothing() {
        let store = SqliteStore::open_memory().unwrap();
        // 中键注入失败：触发器对 field_key = 'b' 的插入 RAISE(ABORT)
        store
            .with_conn::<(), String>(|conn| {
                conn.execute(
                    "CREATE TRIGGER fail_b BEFORE INSERT ON pipeline_state                      WHEN NEW.field_key = 'b' BEGIN SELECT RAISE(ABORT, 'injected mid-key failure'); END;",
                    [],
                )
                .map(|_| ())
                .map_err(|e| e.to_string())
            })
            .unwrap();
        let mut fields = serde_json::Map::new();
        fields.insert("a".to_string(), serde_json::json!(1));
        fields.insert("b".to_string(), serde_json::json!(2));
        fields.insert("c".to_string(), serde_json::json!(3));
        let r = store.upsert_state_fields("pipe_tx", "default", &fields);
        assert!(
            matches!(r, Err(StorageError::Database(ref m)) if m.contains("injected")),
            "中键失败必须整体报错，实际 {r:?}"
        );
        let loaded = store
            .load_pipeline_state("pipe_tx", "default")
            .expect("读回成功");
        assert!(
            loaded.is_empty(),
            "all-or-nothing：中键失败后不得有部分写入，实际 {loaded:?}"
        );

        // 对照组（第二组输入）：无触发器管道批量 3 键全落
        let mut ok_fields = serde_json::Map::new();
        ok_fields.insert("x".to_string(), serde_json::json!("1"));
        ok_fields.insert("y".to_string(), serde_json::json!("2"));
        store
            .upsert_state_fields("pipe_ok", "default", &ok_fields)
            .expect("无故障批量写应成功");
        let loaded = store
            .load_pipeline_state("pipe_ok", "default")
            .expect("读回成功");
        assert_eq!(loaded.len(), 2, "批量成功路径键数完整: {loaded:?}");
        assert_eq!(loaded.get("x"), Some(&serde_json::json!("1")));
    }
}
