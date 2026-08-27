//! SQLite 存储实现
//!
//! 实现 StorageBackend trait，使用 rusqlite 作为 SQLite 后端。
//! 消息层为 op 模型单格式：`message_slots`（纯索引槽位表）+ `blobs`（整条消息
//! 全文，内容寻址去重）——消息持久化走单一真值，无投影链路。
//!
//! [来源: docs/working/adr_engine_design.md §4.2]

/// per-run 易变键：checkpoint 瘦身与冷恢复合并共同跳过的键集（GAP-3）。
///
/// 这些键属于"本轮运行"而非"管道累计状态"——残留会在恢复时覆盖下一轮的
/// 新输入（重启后旧 user 消息被重放消费）。内核恢复侧（api server.rs）经
/// lib.rs 再导出消费同一份，双侧语义单一来源。
///
/// `ended` 与 `suspended` 同属 per-run 终止标志：post 阶段每轮写
/// `ended=true`（pipeline_track），若残留进下一轮 initial_state，引擎
/// `execute_steps`/`execute_body` 见 ended 即短路——冷恢复（registry 丢失）
/// 后 run 秒终 completed、LLM 一次请求都不发。
pub const VOLATILE_RUN_KEYS: &[&str] = &[
    "message",
    "input",
    "message_id",
    "suspended",
    "ended",
    "thinking_strength",
    "_assistant_id_assigned",
    "_pending_message_ops",
    // agent_id 是每轮派发注入键（dispatcher 按线程绑定解析），
    // 不得被 checkpoint/轨迹恢复的历史值覆盖——绑定真值在 agent.id 持久键。
    "agent_id",
];

use std::sync::Arc;

use agentos_core::traits::{MessageQueryOpts, SessionListFilter, StorageBackend};
use agentos_core::types::{
    Branch, MessageRecord, PatchType, PendingInputRecord, PendingInputSource, PipelineRunInfo,
    RunRecord, RunStatus, SessionRecord, StorageError, TraceEntry, UserRecord,
};
use async_trait::async_trait;
use parking_lot::Mutex;
use rusqlite::{Connection, OptionalExtension};
use tracing::{info, warn};

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
-- 密码明文（DEBT: 0.5.0 替换为哈希）。
CREATE TABLE IF NOT EXISTS users (
    user_id       TEXT PRIMARY KEY,
    username      TEXT NOT NULL UNIQUE,
    password      TEXT NOT NULL,
    email         TEXT,
    role          TEXT NOT NULL DEFAULT 'user',
    tenant_id     TEXT NOT NULL DEFAULT 'default',
    created_at    TEXT NOT NULL,
    last_login_at TEXT
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
";

/// 零兼容：检测 message_slots 旧 schema（含内容列，如 content_preview）并 DROP 重建。
///
/// 任务 7 收敛后 slots 是纯索引表；旧库的宽表（含 role/content_preview/tool_calls_json
/// 等内容列）数据格式不再支持——按零兼容原则直接丢弃（承诺过"清库重跑"），不写迁移。
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
        warn!("message_slots 含旧内容列（零兼容），DROP 重建——旧槽位数据被丢弃");
        conn.execute("DROP TABLE message_slots", [])?;
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
/// Clone 语义：浅拷 Arc<Mutex<Connection>>，多个 clone 共享同一连接。
/// spawn_blocking 转发 trait 方法时需要（'self' 借用无法 move 进闭包）。
#[derive(Clone)]
pub struct SqliteStore {
    conn: Arc<Mutex<Connection>>,
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
/// 注意：WAL 模式下损坏可能落在 -wal/-shm 伴生文件中，因此三个文件一起处理。
/// 备份失败（如文件系统权限异常）不在这里阻断——后续重建 open_inner 若仍失败，
/// 错误会正常传播给调用方，不会假装自愈成功。
fn backup_corrupt_files(path: &str) {
    let ts = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| format!("{}.{}", d.as_secs(), d.subsec_nanos()))
        .unwrap_or_else(|_| "unknown".to_string());
    for suffix in ["", "-wal", "-shm"] {
        let src = format!("{path}{suffix}");
        let dst = format!("{src}.corrupt-{ts}");
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
                    "损坏的 SQLite 文件备份失败（继续尝试重建）"
                ),
            }
        }
    }
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

impl SqliteStore {
    /// 在指定路径创建 SQLite 数据库并初始化四表。
    ///
    /// 损坏自愈：若打开/初始化失败且错误为 SQLite 损坏类（malformed / not a database /
    /// corrupt / file is encrypted，如进程异常退出或磁盘故障留下的坏库），自动将损坏文件
    /// （含 -wal/-shm 伴生文件）备份为 `<path>.corrupt-<ts>` 保留现场，然后重建空库继续
    /// 启动——避免 kernel 因单次库损坏直接崩溃退出。
    /// 其他错误（权限、IO 等）原样传播，不做静默降级。
    pub fn open(path: &str) -> Result<Self, StorageError> {
        match Self::open_inner(path) {
            Ok(store) => Ok(store),
            Err(e) if is_corruption_error(&e) => {
                warn!(
                    path = %path,
                    error = %e,
                    "SQLite 数据库损坏，自动备份并重建新库（原数据保留在 .corrupt-* 备份中）"
                );
                backup_corrupt_files(path);
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
        })
    }

    /// 创建内存数据库（用于测试）。
    pub fn open_memory() -> Result<Self, StorageError> {
        let conn = Connection::open_in_memory()?;
        Self::init(&conn)?;
        Ok(Self {
            conn: Arc::new(Mutex::new(conn)),
        })
    }

    fn init(conn: &Connection) -> Result<(), StorageError> {
        conn.execute_batch("PRAGMA journal_mode=WAL;")?;
        // 0.1 投影表退役（不留两套真值）：执行记录/会话消耗账本
        // 由 messages 真值派生读路径替代（调试中心执行记录/LLM 请求页），记忆面归
        // hindsight 自持存储；dynamic_tools 同样退役——动态注册的工具是 state 域
        // 数据不落内核（跨重启由插件自持 state/config 重建）。
        // 四表零生产者；必须在 DDL 之前 DROP——存量残留表结构与现行 DDL 的
        // CREATE INDEX 不兼容会直接炸 init。
        for retired in [
            "execution_records",
            "pipeline_run_summaries",
            "memory",
            "dynamic_tools",
        ] {
            conn.execute(&format!("DROP TABLE IF EXISTS {retired}"), [])?;
        }
        conn.execute_batch(DDL)?;
        migrate_drop_legacy_message_slots(conn)?;
        // 零兼容：0.2 消息真值 = message_slots ⨝ blobs（见文件头注释），旧 messages
        // 投影表已退役。存量库会残留建不建都不管的空表，DROP 掉以保证 db_admin
        // 表清单与后端实际读写一一对应。
        conn.execute("DROP TABLE IF EXISTS messages", [])?;
        migrate_add_tenant_id(conn)?;
        migrate_add_run_pipeline_id(conn)?;
        info!("SQLite four-table store initialized");
        Ok(())
    }

    // ── runs 表操作 ──────────────────────────────────────────

    /// 创建运行实例。
    pub fn create_run(
        &self,
        run_id: &str,
        config_hash: &str,
        tenant_id: &str,
    ) -> Result<(), StorageError> {
        let conn = self.conn.lock();
        let now = chrono::Utc::now().to_rfc3339();
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
    pub fn reap_orphan_runs(&self, tenant_id: &str) -> Result<u64, StorageError> {
        let conn = self.conn.lock();
        let now = chrono::Utc::now().to_rfc3339();
        let rows = conn.execute(
            "UPDATE runs SET status = 'failed', ended_at = COALESCE(ended_at, ?1) \
             WHERE status = 'running' AND tenant_id = ?2",
            rusqlite::params![now, tenant_id],
        )?;
        Ok(rows as u64)
    }

    /// 更新 run 的 metadata 字段（JSON 文本，整体替换）。
    ///
    /// 用于 approval/human_interaction 插件 suspend run 时写入
    /// `pending_interaction_request_id` + `suspend_branch_id` + `suspend_seq`，
    /// 后续 `find_suspended_run_by_request_id` 按此查找并还原 SuspendHandle。
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
    /// （0.1 的 pipeline_run_summaries 投影已退役）。
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
        let Some(s) = raw else {
            return None;
        };
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
    pub fn save_checkpoint(
        &self,
        pipeline_id: &str,
        tenant_id: &str,
        step_no: i64,
        state: &serde_json::Value,
    ) -> Result<(), StorageError> {
        let conn = self.conn.lock();
        let checkpoint_id = format!("cp_{}_{}", pipeline_id, step_no);
        // 瘦身副本：剥离 messages + 易变 per-run 键，写 ckpt_max_seq 水位（原 state 不动）。
        // 易变键（GAP-3）：message/input/message_id/suspended 等属于"本轮运行"，
        // 不是管道累计状态——若残留，冷恢复时会覆盖下一轮的新输入，重启后
        // 旧 user 消息被重放消费。
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

    // ── 域2：session 标签夹内部方法（对齐 0.1 SessionModel）──────────────
    // tenant_id 从 task_local 取。pipeline_ids / metadata 以 JSON 文本存储。

    /// upsert 会话（存在则更新，不存在则插入）。
    /// tenant_id 由调用方在 spawn_blocking 前解析（task_local 不跨 spawn_blocking）。
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

    /// 级联删除会话及其全部关联数据（主管道 + 子任务管道的 messages/traces/runs/state）。
    ///
    /// 通过 `pipeline_sessions` 映射表按 thread_id 找到该会话下所有 pipeline_id
    /// （主管道 + 子管道，无需父子关系），再级联清理它们产生的 messages / traces /
    /// branches / runs，最后删映射表与 sessions 行。
    /// 无记录时同样返回 Ok(())（幂等）。tenant_id 由调用方在 spawn_blocking 前解析。
    /// 单次事务包裹，失败回滚。
    fn delete_session_inner(&self, thread_id: &str, tenant_id: &str) -> Result<(), StorageError> {
        let mut conn = self.conn.lock();
        let tx = conn
            .transaction()
            .map_err(|e| StorageError::Database(format!("begin tx: {e}")))?;

        // 1. 收集该会话全部 pipeline_id：映射表 + sessions.pipeline_ids 兜底（防主管道未写映射）。
        let pipeline_ids = pipeline_ids_for_thread(&tx, thread_id, tenant_id)?;

        if pipeline_ids.is_empty() {
            // 无任何管道（纯标签会话）：直接删 sessions 行
            tx.execute(
                "DELETE FROM sessions WHERE thread_id = ?1 AND tenant_id = ?2",
                rusqlite::params![thread_id, tenant_id],
            )?;
            return tx
                .commit()
                .map_err(|e| StorageError::Database(format!("commit: {e}")));
        }

        // 3. 收集这些 pipeline_id 产生的 run_id（traces/branches/summaries/runs 按 run_id 删）
        let run_ids: Vec<String> = run_ids_of_pipelines(&tx, &pipeline_ids, tenant_id)?;

        if !run_ids.is_empty() {
            Self::delete_in_clause(
                &tx,
                "DELETE FROM traces WHERE run_id IN ({placeholders})",
                &run_ids,
                "",
            )?;
            Self::delete_in_clause(
                &tx,
                "DELETE FROM branches WHERE run_id IN ({placeholders})",
                &run_ids,
                "",
            )?;
            Self::delete_in_clause(
                &tx,
                "DELETE FROM runs WHERE run_id IN ({placeholders})",
                &run_ids,
                "",
            )?;
        }

        // 4. messages 按 pipeline_id 删（含主管道 + 子管道）
        Self::delete_in_clause(
            &tx,
            "DELETE FROM message_slots WHERE pipeline_id IN ({placeholders}) AND tenant_id = ?",
            &pipeline_ids,
            tenant_id,
        )?;

        // 5. 清映射表 + 删 sessions 行
        tx.execute(
            "DELETE FROM pipeline_sessions WHERE thread_id = ?1 AND tenant_id = ?2",
            rusqlite::params![thread_id, tenant_id],
        )?;
        tx.execute(
            "DELETE FROM sessions WHERE thread_id = ?1 AND tenant_id = ?2",
            rusqlite::params![thread_id, tenant_id],
        )?;

        tx.commit()
            .map_err(|e| StorageError::Database(format!("commit: {e}")))
    }

    /// 按 pipeline_id 删除单条管道的全部执行数据（任务删除语义）。
    ///
    /// 0.2 任务 = 管道（GAP-1）：删除任务即删除其管道数据。清理范围对齐
    /// `delete_session_inner` 的级联（runs/traces/branches/message_slots/
    /// pipeline_sessions），另补 `pipeline_state`/`pipeline_checkpoints`
    /// 两张 state 表（会话删除按 thread 级联时这两张表由 clear-all 兜底，
    /// 单管道删除必须自清，否则冷读兜底会从残留 checkpoint 重建幽灵任务）。
    /// 单次事务包裹，失败回滚；无记录时返回 Ok(())（幂等）。
    fn delete_pipeline_inner(
        &self,
        pipeline_id: &str,
        tenant_id: &str,
    ) -> Result<(), StorageError> {
        let mut conn = self.conn.lock();
        let tx = conn
            .transaction()
            .map_err(|e| StorageError::Database(format!("begin tx: {e}")))?;

        // 1. 收集该管道产生的 run_id（traces/branches/runs 按 run_id 删）
        let run_ids: Vec<String> =
            run_ids_of_pipelines(&tx, &[pipeline_id.to_string()], tenant_id)?;

        if !run_ids.is_empty() {
            Self::delete_in_clause(
                &tx,
                "DELETE FROM traces WHERE run_id IN ({placeholders})",
                &run_ids,
                "",
            )?;
            Self::delete_in_clause(
                &tx,
                "DELETE FROM branches WHERE run_id IN ({placeholders})",
                &run_ids,
                "",
            )?;
            Self::delete_in_clause(
                &tx,
                "DELETE FROM runs WHERE run_id IN ({placeholders})",
                &run_ids,
                "",
            )?;
        }

        // 2. messages 按 pipeline_id 删
        tx.execute(
            "DELETE FROM message_slots WHERE pipeline_id = ?1 AND tenant_id = ?2",
            rusqlite::params![pipeline_id, tenant_id],
        )?;

        // 3. state 表（pipeline_state 标量 + checkpoint 快照）——冷读兜底数据源
        tx.execute(
            "DELETE FROM pipeline_state WHERE pipeline_id = ?1 AND tenant_id = ?2",
            rusqlite::params![pipeline_id, tenant_id],
        )?;
        tx.execute(
            "DELETE FROM pipeline_checkpoints WHERE pipeline_id = ?1 AND tenant_id = ?2",
            rusqlite::params![pipeline_id, tenant_id],
        )?;

        // 4. 清映射表（任务管道 thread_id = pipeline_id 自持映射）
        tx.execute(
            "DELETE FROM pipeline_sessions WHERE pipeline_id = ?1 AND tenant_id = ?2",
            rusqlite::params![pipeline_id, tenant_id],
        )?;

        tx.commit()
            .map_err(|e| StorageError::Database(format!("commit: {e}")))
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
    /// tenant_id 由调用方在 spawn_blocking 前解析。
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
    /// tenant_id 由调用方在 spawn_blocking 前解析。
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

    /// 按 thread_id 取单个会话。tenant_id 由调用方在 spawn_blocking 前解析。
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
    /// tenant_id 由调用方在 spawn_blocking 前解析（task_local 不跨 spawn_blocking）。
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
            "INSERT INTO users (user_id, username, password, email, role, tenant_id, created_at, last_login_at)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8)",
            rusqlite::params![
                user.user_id,
                user.username,
                user.password,
                user.email,
                user.role,
                user.tenant_id,
                user.created_at,
                user.last_login_at,
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
            "SELECT user_id, username, password, email, role, tenant_id, created_at, last_login_at
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
            "SELECT user_id, username, password, email, role, tenant_id, created_at, last_login_at
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
            "SELECT user_id, username, password, email, role, tenant_id, created_at, last_login_at
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
        })
    }

    /// 查询某会话下的 step 级轨迹（冷启动统一回放用）。
    ///
    /// 经 pipeline_sessions 映射表按 thread_id 找到全部 pipeline_id → 这些 pipeline 产生
    /// 的 run_id → 对应 traces。只返回 step 级轨迹（plugin_id 为配置 step id，不以
    /// `pipeline_` 前缀的旧插件级轨迹被忽略），按 created_at 升序以便按序 merge 回放。
    /// tenant_id 由调用方在 spawn_blocking 前解析。
    fn get_step_traces_by_thread_inner(
        &self,
        thread_id: &str,
        tenant_id: &str,
    ) -> Result<Vec<TraceEntry>, StorageError> {
        let conn = self.conn.lock();
        // pipeline_id 集合：映射表 + sessions.pipeline_ids 兜底
        let pipeline_ids = pipeline_ids_for_thread(&conn, thread_id, tenant_id)?;
        if pipeline_ids.is_empty() {
            return Ok(vec![]);
        }

        // run_id 集合（经 messages.pipeline_id 反查）
        let run_ids: Vec<String> = run_ids_of_pipelines(&conn, &pipeline_ids, tenant_id)?;
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

    /// spawn_blocking 统一包装：参数先转为 owned 再随闭包移入阻塞线程池，
    /// 同步 `_inner` 方法在池内拿 `&SqliteStore` 执行。
    ///
    /// SqliteStore 用同步 rusqlite，trait 面 async 包装必须走线程池避免阻塞
    /// async runtime；join 失败（阻塞任务 panic / 被取消）统一经 [`join_err`]
    /// 映射为 [`StorageError`]。闭包内的同名词方法解析遵循固有方法优先。
    async fn blocking<F, T>(&self, f: F) -> Result<T, StorageError>
    where
        F: FnOnce(&Self) -> Result<T, StorageError> + Send + 'static,
        T: Send + 'static,
    {
        let this = self.clone();
        tokio::task::spawn_blocking(move || f(&this))
            .await
            .map_err(join_err)?
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

/// spawn_blocking join 失败（阻塞任务 panic / 被取消）→ [`StorageError`] 的统一转换。
/// [`SqliteStore::blocking`]（spawn_blocking 转发固有同步方法）共用。
fn join_err(e: tokio::task::JoinError) -> StorageError {
    StorageError::Database(format!("join error: {e}"))
}

#[async_trait]
impl StorageBackend for SqliteStore {
    async fn get_run(&self, run_id: &str) -> Result<RunRecord, StorageError> {
        let tenant_id = agentos_tenant::current_or_default("default").tenant_id;
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
                    status: match status_str.as_str() {
                        "running" => RunStatus::Running,
                        "suspended" => RunStatus::Suspended,
                        "completed" => RunStatus::Completed,
                        "failed" => RunStatus::Failed,
                        _ => RunStatus::Running,
                    },
                    tenant_id: row.get(3)?,
                    created_at: row.get(4)?,
                    ended_at: row.get(5)?,
                    current_branch: row.get(6)?,
                    current_seq: row.get::<_, i64>(7)? as u32,
                    metadata: metadata_str
                        .and_then(|s| serde_json::from_str(&s).ok()),
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
    async fn set_run_pipeline(&self, run_id: &str, pipeline_id: &str) -> Result<(), StorageError> {
        let conn = self.conn.lock();
        conn.execute(
            "UPDATE runs SET pipeline_id = ?1 WHERE run_id = ?2",
            rusqlite::params![pipeline_id, run_id],
        )?;
        Ok(())
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
        let conn = self.conn.lock();
        let mut stmt = conn.prepare(
            "SELECT run_id, config_hash, status, tenant_id, created_at, ended_at, current_branch, current_seq, metadata FROM runs WHERE pipeline_id = ?1 AND tenant_id = ?2 ORDER BY created_at DESC",
        )?;
        let rows = stmt.query_map(rusqlite::params![pipeline_id, tenant_id], |row| {
            let status_str: String = row.get(2)?;
            let metadata_str: Option<String> = row.get(8)?;
            Ok(RunRecord {
                run_id: row.get(0)?,
                config_hash: row.get(1)?,
                status: match status_str.as_str() {
                    "running" => RunStatus::Running,
                    "suspended" => RunStatus::Suspended,
                    "completed" => RunStatus::Completed,
                    "failed" => RunStatus::Failed,
                    _ => RunStatus::Running,
                },
                tenant_id: row.get(3)?,
                created_at: row.get(4)?,
                ended_at: row.get(5)?,
                current_branch: row.get(6)?,
                current_seq: row.get(7)?,
                metadata: metadata_str.and_then(|s| serde_json::from_str(&s).ok()),
            })
        })?;
        Ok(rows.collect::<Result<Vec<_>, _>>()?)
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

    async fn append_trace(&self, entry: TraceEntry) -> Result<(), StorageError> {
        let tenant_id = agentos_tenant::current_or_default("default").tenant_id;
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

    async fn create_branch(&self, branch: Branch) -> Result<(), StorageError> {
        let tenant_id = agentos_tenant::current_or_default("default").tenant_id;
        let conn = self.conn.lock();
        conn.execute(
            "INSERT INTO branches (branch_id, run_id, parent_branch, parent_seq, tenant_id, created_at) VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
            rusqlite::params![
                branch.branch_id,
                branch.run_id,
                branch.parent_branch,
                branch.parent_seq.map(|s| s as i64),
                tenant_id,
                branch.created_at,
            ],
        )?;
        Ok(())
    }

    async fn update_run_status(
        &self,
        run_id: &str,
        status: RunStatus,
        current_branch: Option<&str>,
        current_seq: Option<u32>,
    ) -> Result<(), StorageError> {
        let tenant_id = agentos_tenant::current_or_default("default").tenant_id;
        let conn = self.conn.lock();
        let status_str = match status {
            RunStatus::Running => "running",
            RunStatus::Suspended => "suspended",
            RunStatus::Completed => "completed",
            RunStatus::Failed => "failed",
        };
        let now = chrono::Utc::now().to_rfc3339();

        match (current_branch, current_seq) {
            (Some(branch), Some(seq)) => {
                let ended = if status == RunStatus::Completed || status == RunStatus::Failed {
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
                let ended = if status == RunStatus::Completed || status == RunStatus::Failed {
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

    // ── 域10：分层持久化投影（trait async 包装，spawn_blocking + task_local tenant）──

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

    // ── 域2：session 标签夹 CRUD（对齐 0.1 SessionModel）──────────────
    // 注意：tenant_id 必须在 spawn_blocking 之前解析——tokio::task_local 不跨 spawn_blocking。
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

    async fn delete_session(&self, thread_id: &str) -> Result<(), StorageError> {
        let tenant_id = agentos_tenant::current_or_default("default").tenant_id;
        let thread_id = thread_id.to_string();
        self.blocking(move |this| this.delete_session_inner(&thread_id, &tenant_id))
            .await
    }

    async fn delete_pipeline(&self, pipeline_id: &str) -> Result<(), StorageError> {
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

    // ── 域6：users async wrapper（0.5.0 最小持久化地基）──────────────
    // 注意：get_user_by_username / list_users 跨租户查询，不解析 task_local tenant。
    // get_user_by_id 按 tenant 隔离（与消息/会话一致，task_local 在 spawn_blocking 前解析）。

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

    /// 回归测试：损坏的 SQLite 文件不应导致 kernel 启动崩溃（issue: kernel 启动后 /health 不响应）。
    ///
    /// 背景：`.kernel_02.log` 末行 `Error: Database("database disk image is malformed")` ——
    /// 进程异常退出/磁盘故障可能留下损坏的 agentos_kernel.db，`SqliteStore::open()` 直接
    /// 返回 Err，main 传播退出，/health 永远不响应，启动脚本 60s 轮询超时。
    ///
    /// 期望行为：open 检测到损坏类错误时自愈——备份损坏文件保留现场，重建空库返回 Ok，
    /// 新库可正常读写（create_run / get_run）。
    #[tokio::test]
    async fn test_open_corrupt_db_self_heals() {
        // 模拟损坏的 SQLite 文件：文件头不是 "SQLite format 3"（进程崩溃/磁盘异常场景）
        let dir = tempfile::tempdir().unwrap();
        let db_path = dir.path().join("agentos_kernel.db");
        std::fs::write(&db_path, vec![0xDE_u8; 8192]).unwrap();

        // open 不应报错退出，而应自愈：返回 Ok 且新库可正常读写
        let store = SqliteStore::open(db_path.to_str().unwrap()).unwrap();
        store
            .create_run("run_self_heal", "hash_abc", "default")
            .unwrap();
        let run = store.get_run("run_self_heal").await.unwrap();
        assert_eq!(run.run_id, "run_self_heal");

        // 损坏文件应被备份保留现场（agentos_kernel.db.corrupt-*），便于人工排查
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
    /// pipeline_sessions，单事务；无记录时幂等返回 Ok。
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
    /// 对齐 0.1 SessionModel：会话是聚合管道引用的标签夹。
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

        // upsert 更新：追加子管道 pid_sub，但 active 不动（对齐 0.1 set_active=False）
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
    async fn test_append_trace_and_replay() {
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

    #[tokio::test]
    async fn test_create_branch() {
        let store = SqliteStore::open_memory().unwrap();
        store.create_run("run_6", "hash", "default").unwrap();

        let branch = Branch {
            branch_id: "main.rollback.001".to_string(),
            run_id: "run_6".to_string(),
            parent_branch: Some("main".to_string()),
            parent_seq: Some(1),
            created_at: chrono::Utc::now().to_rfc3339(),
        };
        store.create_branch(branch).await.unwrap();
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
}
