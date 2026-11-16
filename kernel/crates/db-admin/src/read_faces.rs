//! db-read 内核库读契约服务——插件/脚本只读访问内核 SQLite 的唯一通道（C2/C3 刀1）。
//!
//! 契约唯一形态 = 内核 Rust 读契约（ADR 草案 2026-10-15，对照基线 commit
//! 71dca0fff 的 Python 参照实现）：数据面（face）声明式注册（
//! `config/kernel/read_faces.yaml`，加面 = 改配置不动机制代码）+ 自由只读
//! SELECT + 租户内建收窄 + 权限分档（tenant | global-admin）。DB 文件句柄
//! 只存在于内核进程，服务边界外只出行数据。
//!
//! ## 两个入口，一套执法
//!
//! | 入口 | 消费方 | 身份来源 |
//! |---|---|---|
//! | capability 面 `db-read.query/faces`（本模块 [`DbReadCapabilityHandler`]，
//!   经 api crate capability_router 的 `("db-read", m)` 内置臂路由） |
//!   monitoring / approval / cost_control 插件 | 插件把入站 `Authorization`
//!   原样放 `params._authorization`，内核侧重建 HeaderMap 复权（
//!   `resolve_request_user`，db-admin capability.rs 同款先例）——不接收
//!   `role`/`tenant_id` 形参，传入即拒 |
//! | HTTP 管理端点 `POST /api/v1/db/read`（本模块 [`db_read_http_handler`]，
//!   由 api server.rs 一行挂载） | mode_evolution 运维脚本、人工运维 |
//!   内核认证会话权威解析（Bearer → `resolve_request_user`） |
//!
//! 两入口汇合同一执法函数 [`query`]，入口只做身份桥接。
//!
//! ## 执法机制（与 spec §3.4 一致）
//!
//! 1. **只读与面限制**：每请求一连接（`SQLITE_OPEN_READ_ONLY`，authorizer 是
//!    per-connection 状态，µs 级开连接，内核库 WAL 模式并发只读安全；库缺失
//!    即 open 期报错，绝不静默建库）+ authorizer 只放行 SELECT / 函数调用 /
//!    声明面那张表的 READ（写/PRAGMA/ATTACH/他表含跨面 join 一律 DENY）+
//!    语句级文本校验仅单条 SELECT。
//!    [偏差注记] rusqlite 0.32 的 API 名是 `Connection::authorizer(Some(..))`
//!    （spec 写作 set_authorizer 为 SQLite C API 名）——同一机制。
//! 2. **租户内建收窄**：tenant 档强制包装
//!    `SELECT * FROM (调用方SELECT) AS _scoped WHERE <租户列> = :__tenant__`，
//!    谓词值只能来自身份链解析的 tenant_id；收窄查询须投影面表真实租户列
//!    （未投影 = no such column；常量改名冒充 = authorizer READ 列记录校验拒绝）。
//! 3. **权限分档**：tenant 档 = 按调用方租户收窄（持租户身份即可，结果恒 ⊆
//!    本租户行）；global 档 = 仅 admin，可带 `tenant` 参数收窄单租户。
//!    [偏差注记] Python 基线的 tenant 档要求 `role == "tenant"`，但内核角色
//!    词表是 admin/viewer/user（无 "tenant" 角色）——按 spec §1"tenant 档 =
//!    结果强制按调用方租户收窄"落地为持租户身份即可，收窄不可绕不变。
//! 4. **连接不外泄**：函数只返回行数据。
//!
//! ## 裸聚合形状纪律（调用方文档约定，机制层不做 SQL 形状分析）
//!
//! 无 GROUP BY 的裸聚合把全租户聚成一行（租户列成任意值），包装无法正确
//! 收窄——聚合必须 `GROUP BY <租户列>` 后交包装收窄（诚实降级，ADR 影响 §）。
//!
//! [来源: docs/working/内核库读契约服务设计_spec_20261015.md §3/§5]
//! [来源: docs/working/ADR草案_内核库读契约服务_20261015.md]

use std::collections::HashMap;
use std::path::Path;
use std::sync::{Arc, OnceLock};

use agentos_engine::store::SQLITE_BUSY_TIMEOUT_MS;
use agentos_http::auth::resolve_request_user;
use agentos_http::error::ApiError;
use agentos_mcp::McpError;
use axum::body::Bytes;
use axum::http::{header::AUTHORIZATION, HeaderMap, HeaderValue};
use axum::response::{IntoResponse, Response};
use rusqlite::hooks::{AuthAction, AuthContext, Authorization};
use rusqlite::types::{Value as SqlValue, ValueRef};
use rusqlite::OpenFlags;
use serde_json::{json, Value};
use tokio::task::spawn_blocking;

/// capability namespace（db-read 只读数据面）。
pub const NAMESPACE: &str = "db-read";

/// 契约注入的租户谓词值独占名：调用方参数占用即拒（谓词值只能来自身份链）。
const TENANT_PARAM: &str = "__tenant__";

/// 鉴权身份保留形参名：插件不可传（身份从内核认证会话取，传入即拒）。
const IDENTITY_PARAM_KEYS: [&str; 2] = ["role", "tenant_id"];

// ── 错误码（spec §5，两入口同码） ─────────────────────────────────────

/// 面未注册（404）。
pub const ERR_FACE_NOT_FOUND: &str = "DB_READ_FACE_NOT_FOUND";
/// 权限分档裁决失败 / 身份形参传入（403）。
pub const ERR_ROLE_DENIED: &str = "DB_READ_ROLE_DENIED";
/// 语句级拒绝（非 SELECT/多语句/authorizer DENY，422）。
pub const ERR_SQL_REJECTED: &str = "DB_READ_SQL_REJECTED";
/// 参数占用保留名 `__tenant__`（422）。
pub const ERR_RESERVED_PARAM: &str = "DB_READ_RESERVED_PARAM";
/// 收窄查询未投影真实租户列 / 常量冒充（422）。
pub const ERR_TENANT_COLUMN_REQUIRED: &str = "DB_READ_TENANT_COLUMN_REQUIRED";
/// 库缺失/损坏/锁忙等存储层故障（503/500）。
pub const ERR_STORAGE: &str = "DB_READ_STORAGE";
/// 认证失败（401；凭证缺失/无效/过期，复权链原样语义）。
pub const ERR_NOT_AUTHENTICATED: &str = "DB_READ_NOT_AUTHENTICATED";

// ── 数据面注册表 ─────────────────────────────────────────────────────

/// 数据面权限档：tenant = 结果强制按调用方租户收窄；global = 仅 admin 可调。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum FaceScope {
    /// 租户收窄档。
    Tenant,
    /// 管理员全局面（可带 tenant 收窄单租户）。
    Global,
}

impl FaceScope {
    fn parse(s: &str) -> Option<Self> {
        match s {
            "tenant" => Some(Self::Tenant),
            "global" => Some(Self::Global),
            _ => None,
        }
    }

    fn as_str(self) -> &'static str {
        match self {
            Self::Tenant => "tenant",
            Self::Global => "global",
        }
    }
}

/// 一个注册数据面：暴露的表名 + 权限档 + 租户列（收窄谓词与真实投影校验依据）。
#[derive(Clone, Debug)]
pub struct DataFace {
    /// 面 id（调用方引用名；同表可注册多面，如 pipeline_state 双档）。
    pub id: String,
    /// 暴露的物理表名（authorizer READ 放行白名单依据）。
    pub table: String,
    /// 权限档。
    pub scope: FaceScope,
    /// 面表的租户列名（包装谓词列；装载期校验为安全标识符后拼 SQL）。
    pub tenant_column: String,
}

/// read_faces.yaml 文件形状（serde 宽松：未知字段忽略，前向兼容）。
#[derive(serde::Deserialize, Default)]
struct FacesFile {
    #[serde(default)]
    faces: Vec<FaceEntry>,
}

#[derive(serde::Deserialize)]
struct FaceEntry {
    id: String,
    table: String,
    scope: String,
    tenant_column: String,
}

/// face 注册表 + 内核库路径（装载一次，随请求只读）。
#[derive(Clone, Debug)]
pub struct ReadFacesRegistry {
    faces: HashMap<String, DataFace>,
    db_path: String,
}

/// 标识符白名单（表名/租户列拼 SQL 前的机械校验，fail-closed）。
fn is_safe_identifier(s: &str) -> bool {
    !s.is_empty()
        && s.chars()
            .next()
            .is_some_and(|c| c.is_ascii_alphabetic() || c == '_')
        && s.chars().all(|c| c.is_ascii_alphanumeric() || c == '_')
}

impl ReadFacesRegistry {
    /// 装载 `config/kernel/read_faces.yaml`（config_root = config/ 目录）。
    ///
    /// 文件缺失 = 未配置（空注册表，启动继续——对齐 storage.yaml 缺失宽松
    /// 先例；全部查询按面未注册拒绝）；文件存在但损坏/校验不过 = fail-closed
    /// 拒绝启动（Err 上抛）。
    pub fn load(config_root: &Path, db_path: &str) -> Result<Self, String> {
        let path = config_root.join("kernel").join("read_faces.yaml");
        let raw = match std::fs::read_to_string(&path) {
            Ok(raw) => raw,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
                return Ok(Self {
                    faces: HashMap::new(),
                    db_path: db_path.to_string(),
                });
            }
            Err(e) => return Err(format!("read_faces.yaml 读取失败: {e}")),
        };
        Self::from_yaml(&raw, db_path).map_err(|e| format!("{}: {e}", path.display()))
    }

    /// 解析并校验 YAML 文本（装载期 fail-closed：scope 词表/面 id 重复/标识符
    /// 合法性；同表可注册多面）。
    pub fn from_yaml(yaml: &str, db_path: &str) -> Result<Self, String> {
        let file: FacesFile =
            serde_yaml::from_str(yaml).map_err(|e| format!("YAML 解析失败: {e}"))?;
        let mut faces = HashMap::new();
        for entry in file.faces {
            if !is_safe_identifier(&entry.id) {
                return Err(format!("面 id 非法（须为安全标识符）: {:?}", entry.id));
            }
            if !is_safe_identifier(&entry.table) {
                return Err(format!(
                    "面 {:?} 的表名非法（须为安全标识符）: {:?}",
                    entry.id, entry.table
                ));
            }
            if !is_safe_identifier(&entry.tenant_column) {
                return Err(format!(
                    "面 {:?} 的租户列非法（须为安全标识符）: {:?}",
                    entry.id, entry.tenant_column
                ));
            }
            let scope = FaceScope::parse(&entry.scope).ok_or_else(|| {
                format!(
                    "面 {:?} 的 scope 非法（词表 tenant|global）: {:?}",
                    entry.id, entry.scope
                )
            })?;
            let face = DataFace {
                id: entry.id.clone(),
                table: entry.table,
                scope,
                tenant_column: entry.tenant_column,
            };
            let id = entry.id.clone();
            if faces.insert(id.clone(), face).is_some() {
                return Err(format!("面 id 重复: {id:?}"));
            }
        }
        Ok(Self {
            faces,
            db_path: db_path.to_string(),
        })
    }

    /// 按面 id 取注册面。
    pub fn face(&self, id: &str) -> Option<&DataFace> {
        self.faces.get(id)
    }

    /// 当前注册表快照（`faces` 自省面用；按 id 排序保证输出稳定）。
    pub fn list(&self) -> Vec<&DataFace> {
        let mut all: Vec<&DataFace> = self.faces.values().collect();
        all.sort_by(|a, b| a.id.cmp(&b.id));
        all
    }

    /// 内核库文件路径（`:memory:` 等别名原样；只读连接打开）。
    pub fn db_path(&self) -> &str {
        &self.db_path
    }
}

// ── 身份与错误 ───────────────────────────────────────────────────────

/// 调用方身份（两入口各自解析后传入；档位裁决依据）。
#[derive(Clone, Debug)]
pub struct DbReadIdentity {
    /// 租户 ID（tenant 档收窄值；空 = 无租户身份，tenant 档拒绝）。
    pub tenant_id: String,
    /// 角色（global 档仅 "admin"）。
    pub role: String,
}

/// db-read 契约错误（spec §5：两入口同码，HTTP 端点直接映射状态码）。
#[derive(Debug)]
pub struct DbReadError {
    /// HTTP 状态码语义（404/403/422/503/500）。
    pub status: u16,
    /// 错误码（`DB_READ_*` 常量）。
    pub code: &'static str,
    /// 用户可读信息。
    pub message: String,
}

impl DbReadError {
    fn new(status: u16, code: &'static str, message: impl Into<String>) -> Self {
        Self {
            status,
            code,
            message: message.into(),
        }
    }

    fn face_not_found(face: &str) -> Self {
        Self::new(
            404,
            ERR_FACE_NOT_FOUND,
            format!("未登记数据面 {face:?}（面外查询一律拒绝）"),
        )
    }

    fn role_denied(message: impl Into<String>) -> Self {
        Self::new(403, ERR_ROLE_DENIED, message)
    }

    fn unauthenticated(message: impl Into<String>) -> Self {
        Self::new(401, ERR_NOT_AUTHENTICATED, message)
    }

    fn sql_rejected(message: impl Into<String>) -> Self {
        Self::new(422, ERR_SQL_REJECTED, message)
    }

    fn storage(status: u16, message: impl Into<String>) -> Self {
        Self::new(status, ERR_STORAGE, message)
    }

    fn from_auth_error(e: ApiError) -> Self {
        let status = api_status(&e);
        if status == 401 {
            Self::unauthenticated(e.to_string())
        } else {
            Self::storage(status, e.to_string())
        }
    }
}

/// 查询结果：列名与行数据（行内值按列序对齐；连接不外泄）。
#[derive(Debug, PartialEq)]
pub struct DbReadResult {
    pub columns: Vec<String>,
    pub rows: Vec<Vec<Value>>,
}

// ── 执法核心 ─────────────────────────────────────────────────────────

/// 每请求一只读连接（authorizer per-connection；库缺失即 open 期报错，
/// 绝不静默建库——读写连接会静默建空库的副作用在契约层被只读 URI 关死）。
/// busy_timeout 与引擎连接同口径（[`agentos_engine::store::SQLITE_BUSY_TIMEOUT_MS`]）：
/// 遇 WAL checkpoint 等短暂持锁等待而非立即 SQLITE_BUSY（多连接并发防护面）。
fn open_read_only_conn(db_path: &str) -> Result<rusqlite::Connection, DbReadError> {
    let conn = rusqlite::Connection::open_with_flags(
        db_path,
        OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX,
    )
    .map_err(|e| DbReadError::storage(503, format!("内核库只读打开失败: {e}")))?;
    conn.execute_batch(&format!("PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS};"))
        .map_err(|e| DbReadError::storage(503, format!("busy_timeout 设置失败: {e}")))?;
    Ok(conn)
}

/// 在声明数据面上执行自由只读 SELECT，返回全部行列；连接不外泄。
///
/// 阻塞 SQLite 调用——调用方（capability/HTTP 入口）负责 `spawn_blocking`。
/// 收窄语义（spec §3.5）：tenant 档结果恒 ⊆ 面表 `tenant_column` = 身份
/// tenant 的行，与调用方 SQL 内容无关（自写内层谓词只会过度限制）。
pub fn query(
    registry: &ReadFacesRegistry,
    face_id: &str,
    sql: &str,
    params: &Value,
    identity: &DbReadIdentity,
    tenant_override: Option<&str>,
) -> Result<DbReadResult, DbReadError> {
    let face = registry
        .face(face_id)
        .ok_or_else(|| DbReadError::face_not_found(face_id))?;

    // 档位裁决（fail-closed）：tenant 档缺租户身份绝不退化为全租户；
    // tenant 收窄值不可被调用方指派（tenant_override 仅 global+admin 合法）。
    let narrowing_target: Option<String> = match face.scope {
        FaceScope::Tenant => {
            if tenant_override.is_some() {
                return Err(DbReadError::role_denied(
                    "tenant 档面不接受 tenant 参数（收窄值只能来自调用方身份）",
                ));
            }
            if identity.tenant_id.is_empty() {
                return Err(DbReadError::role_denied(format!(
                    "租户档面 {face_id:?} 要求租户身份（缺租户不退化全租户）"
                )));
            }
            Some(identity.tenant_id.clone())
        }
        FaceScope::Global => {
            if identity.role != "admin" {
                return Err(DbReadError::role_denied(format!(
                    "全局面 {face_id:?} 仅 admin 可查（得到 {:?}）",
                    identity.role
                )));
            }
            tenant_override
                .filter(|t| !t.is_empty())
                .map(str::to_string)
        }
    };

    // 语句级文本校验：单条 SELECT（写/PRAGMA/多语句/CTE 开头文本一律拒；
    // 值走绑定占位符，SQL 文本含分号字面量一并拒绝）。
    let text = sql.trim();
    if !text.to_ascii_lowercase().starts_with("select") {
        let mut head: String = sql.chars().take(40).collect();
        if sql.chars().count() > 40 {
            head.push('…');
        }
        return Err(DbReadError::sql_rejected(format!(
            "只收只读 SELECT（得到 {head:?}）"
        )));
    }
    let text = text.strip_suffix(';').map(str::trim_end).unwrap_or(text);
    if text.contains(';') {
        return Err(DbReadError::sql_rejected(
            "只收单条 SELECT（多语句拒绝；分号请走绑定参数）",
        ));
    }

    // 绑定参数形态：命名 dict（与契约注入占位共存）或无参；位置参数仅在
    // 不收窄（global 全局面）时可用（位置占位与注入命名占位不可混）。
    let narrowing = narrowing_target.is_some();
    let mut named: Vec<(String, SqlValue)> = Vec::new();
    let mut positional: Vec<SqlValue> = Vec::new();
    match params {
        Value::Null => {}
        Value::Object(map) => {
            for (k, v) in map {
                if narrowing && k == TENANT_PARAM {
                    return Err(DbReadError::new(
                        422,
                        ERR_RESERVED_PARAM,
                        format!("参数不得占用保留名 {TENANT_PARAM}（谓词值不可被指派）"),
                    ));
                }
                named.push((k.clone(), json_to_sql(v)?));
            }
        }
        Value::Array(arr) => {
            if narrowing {
                return Err(DbReadError::sql_rejected(
                    "收窄查询参数须为命名 dict（契约注入占位与位置占位不可混；无参传 null）",
                ));
            }
            for v in arr {
                positional.push(json_to_sql(v)?);
            }
        }
        _ => {
            return Err(DbReadError::sql_rejected(
                "params 须为 JSON 对象（命名参数）、数组（位置参数，仅全局面）或 null",
            ));
        }
    }

    let exec_sql = match (&narrowing_target, face.tenant_column.as_str()) {
        // 收窄值经绑定参数注入（:__tenant__），SQL 文本只挂谓词列。
        (Some(_), col) => {
            format!("SELECT * FROM ({text}) AS _scoped WHERE {col} = :{TENANT_PARAM}")
        }
        (None, _) => text.to_string(),
    };

    let conn = open_read_only_conn(registry.db_path())?;

    // authorizer：放行 SELECT / 函数调用 / 声明面表的 READ（逐列记录列名，
    // 供收窄查询拒绝"常量改名冒充租户列"）；写/PRAGMA/ATTACH/他表一律 DENY。
    let read_columns: Arc<std::sync::Mutex<Vec<String>>> = Arc::default();
    let auth_table = face.table.clone();
    let auth_sink = Arc::clone(&read_columns);
    conn.authorizer(Some(move |ctx: AuthContext<'_>| match ctx.action {
        AuthAction::Select | AuthAction::Function { .. } => Authorization::Allow,
        AuthAction::Read {
            table_name,
            column_name,
        } if table_name.eq_ignore_ascii_case(&auth_table) => {
            if !column_name.is_empty() {
                auth_sink
                    .lock()
                    .expect("authorizer 列记录锁")
                    .push(column_name.to_ascii_lowercase());
            }
            Authorization::Allow
        }
        _ => Authorization::Deny,
    }));

    let mut stmt = conn
        .prepare(&exec_sql)
        .map_err(|e| map_prepare_error(e, narrowing, &face.tenant_column))?;
    let columns: Vec<String> = stmt
        .column_names()
        .into_iter()
        .map(str::to_string)
        .collect();
    let column_count = stmt.column_count();

    // 绑定：命名键归一为 `:key`（SQLite 绑定名须带前缀；与调用方 SQL 的
    // :name 占位拼写一致）；收窄时追加契约注入的 :__tenant__。
    let mut bind_names: Vec<String> = named.iter().map(|(k, _)| format!(":{k}")).collect();
    let mut bind_values: Vec<SqlValue> = named.into_iter().map(|(_, v)| v).collect();
    if let Some(tenant) = &narrowing_target {
        bind_names.push(format!(":{TENANT_PARAM}"));
        bind_values.push(SqlValue::Text(tenant.clone()));
    }
    let mut rows_out: Vec<Vec<Value>> = Vec::new();
    if narrowing || !bind_names.is_empty() {
        // 动态绑定面：&dyn ToSql 切片（rusqlite Params 的命名切片形态）。
        let binds: Vec<(&str, &dyn rusqlite::ToSql)> = bind_names
            .iter()
            .map(String::as_str)
            .zip(bind_values.iter())
            .map(|(name, value)| (name, value as &dyn rusqlite::ToSql))
            .collect();
        let mut rows = stmt
            .query(binds.as_slice())
            .map_err(|e| DbReadError::sql_rejected(format!("查询执行失败: {e}")))?;
        collect_rows(&mut rows, column_count, &mut rows_out)?;
    } else {
        let mut rows = stmt
            .query(rusqlite::params_from_iter(positional.iter()))
            .map_err(|e| DbReadError::sql_rejected(format!("查询执行失败: {e}")))?;
        collect_rows(&mut rows, column_count, &mut rows_out)?;
    }

    // 收窄查询必须投影面表真实租户列：常量改名冒充该列会让包装谓词恒真
    // （全租户行按冒充值过滤）——authorizer READ 列记录里没有真实列即拒绝。
    if narrowing {
        let col = face.tenant_column.to_ascii_lowercase();
        let read = read_columns.lock().expect("authorizer 列记录锁");
        if !read.contains(&col) {
            return Err(DbReadError::new(
                422,
                ERR_TENANT_COLUMN_REQUIRED,
                format!(
                    "面 {face_id:?} 收窄查询必须投影真实列 {:?}（常量改名冒充拒绝）",
                    face.tenant_column
                ),
            ));
        }
    }
    Ok(DbReadResult {
        columns,
        rows: rows_out,
    })
}

/// prepare 期错误映射：authorizer DENY → 422（SQLite 报文自带被拒对象名）；
/// 收窄查询未投影租户列（no such column）→ 422 专码；其余语句错误 → 422。
fn map_prepare_error(e: rusqlite::Error, narrowing: bool, tenant_column: &str) -> DbReadError {
    let text = e.to_string();
    if text.contains("not authorized") || text.contains("prohibited") {
        return DbReadError::sql_rejected(format!(
            "authorizer DENY（面外表/写/PRAGMA 不可达）: {text}"
        ));
    }
    if narrowing && text.contains("no such column") {
        return DbReadError::new(
            422,
            ERR_TENANT_COLUMN_REQUIRED,
            format!("收窄查询未投影租户列 {tenant_column:?}（包装谓词无列可挂）: {text}"),
        );
    }
    DbReadError::sql_rejected(format!("SQL 准备失败: {text}"))
}

/// 逐行收集（ValueRef → JSON；Blob 非本契约消费面，按不可读拒绝）。
fn collect_rows(
    rows: &mut rusqlite::Rows<'_>,
    column_count: usize,
    out: &mut Vec<Vec<Value>>,
) -> Result<(), DbReadError> {
    while let Some(row) = rows
        .next()
        .map_err(|e| DbReadError::storage(500, format!("行读取失败: {e}")))?
    {
        let mut r = Vec::with_capacity(column_count);
        for i in 0..column_count {
            let v = row
                .get_ref(i)
                .map_err(|e| DbReadError::storage(500, format!("列读取失败: {e}")))?;
            r.push(value_ref_to_json(v)?);
        }
        out.push(r);
    }
    Ok(())
}

/// SQLite 值 → JSON（Integer/Real/Text/Null 直映；非有限浮点降级 null）。
fn value_ref_to_json(v: ValueRef<'_>) -> Result<Value, DbReadError> {
    Ok(match v {
        ValueRef::Null => Value::Null,
        ValueRef::Integer(i) => json!(i),
        ValueRef::Real(f) => {
            if f.is_finite() {
                json!(f)
            } else {
                Value::Null
            }
        }
        ValueRef::Text(t) => json!(String::from_utf8_lossy(t)),
        ValueRef::Blob(_) => {
            return Err(DbReadError::storage(
                500,
                "BLOB 列不在 db-read 契约面（行数据走 JSON 边界）；如需读 blob \
                 请走 db-admin 表接口",
            ));
        }
    })
}

/// JSON 绑定值 → SQLite 值（数组/对象不可绑定，422）。
fn json_to_sql(v: &Value) -> Result<SqlValue, DbReadError> {
    Ok(match v {
        Value::Null => SqlValue::Null,
        Value::Bool(b) => SqlValue::Integer(*b as i64),
        Value::Number(n) => {
            if let Some(i) = n.as_i64() {
                SqlValue::Integer(i)
            } else if let Some(f) = n.as_f64() {
                SqlValue::Real(f)
            } else {
                return Err(DbReadError::sql_rejected(format!(
                    "数值参数越界不可绑定: {n}"
                )));
            }
        }
        Value::String(s) => SqlValue::Text(s.clone()),
        _ => {
            return Err(DbReadError::sql_rejected(
                "数组/对象不可作绑定参数（SQL 内用 json 函数展开）",
            ));
        }
    })
}

// ── 入口 1：capability 面（db-read.query / db-read.faces） ────────────

/// `db-read` namespace 的 capability handler（经 api capability_router 内置臂
/// `("db-read", m)` 路由；G6 授权闸在 router 单点先行）。
pub struct DbReadCapabilityHandler {
    /// 用户/租户解析用存储后端（api `AppState.store` 同一实例）。
    store: Option<Arc<dyn agentos_core::traits::StorageBackend>>,
    /// face 注册表（boot 期从 config/kernel/read_faces.yaml 装载）。
    registry: Arc<ReadFacesRegistry>,
}

impl DbReadCapabilityHandler {
    /// 创建 handler。
    pub fn new(
        store: Option<Arc<dyn agentos_core::traits::StorageBackend>>,
        registry: Arc<ReadFacesRegistry>,
    ) -> Self {
        Self { store, registry }
    }

    /// 从 params 的 `_authorization`（HTTP 面插件转发的原始 Authorization 头值）
    /// 重建 HeaderMap，复用内核 resolve_request_user 复权（不信插件转述值）。
    fn auth_headers(params: &Value) -> HeaderMap {
        let mut headers = HeaderMap::new();
        if let Some(auth) = params.get("_authorization").and_then(|v| v.as_str()) {
            if let Ok(v) = HeaderValue::from_str(auth) {
                headers.insert(AUTHORIZATION, v);
            }
        }
        headers
    }

    /// 身份链：内核侧从 token/store 权威解析（缺凭证 401 语义原样上抛）。
    async fn identity(&self, params: &Value) -> Result<DbReadIdentity, DbReadError> {
        let headers = Self::auth_headers(params);
        let (_, _, role, tenant_id) = resolve_request_user(self.store.as_ref(), &headers)
            .await
            .map_err(DbReadError::from_auth_error)?;
        Ok(DbReadIdentity { tenant_id, role })
    }

    /// 拒绝身份保留形参（插件不可传 role/tenant_id——身份只来自内核复权）。
    fn reject_identity_params(params: &Value) -> Result<(), DbReadError> {
        for key in IDENTITY_PARAM_KEYS {
            if params.get(key).is_some() {
                return Err(DbReadError::role_denied(format!(
                    "参数 {key:?} 不可由调用方传入（身份由内核认证会话权威解析）"
                )));
            }
        }
        Ok(())
    }

    async fn query(&self, params: &Value) -> Result<(u16, Value), DbReadError> {
        Self::reject_identity_params(params)?;
        let identity = self.identity(params).await?;
        let face = params
            .get("face")
            .and_then(|v| v.as_str())
            .ok_or_else(|| DbReadError::sql_rejected("缺少 face 参数"))?
            .to_string();
        let sql = params
            .get("sql")
            .and_then(|v| v.as_str())
            .ok_or_else(|| DbReadError::sql_rejected("缺少 sql 参数"))?
            .to_string();
        let bind_params = params.get("params").cloned().unwrap_or(Value::Null);
        let tenant_override = params
            .get("tenant")
            .and_then(|v| v.as_str())
            .map(str::to_string);
        let registry = Arc::clone(&self.registry);
        let result = spawn_blocking(move || {
            query(
                &registry,
                &face,
                &sql,
                &bind_params,
                &identity,
                tenant_override.as_deref(),
            )
        })
        .await
        .map_err(|e| DbReadError::storage(500, format!("数据库任务失败: {e}")))??;
        Ok((
            200,
            json!({ "columns": result.columns, "rows": result.rows }),
        ))
    }

    async fn faces(&self, params: &Value) -> Result<(u16, Value), DbReadError> {
        // 自省/调试面：要求有效身份（复权失败 401 原样上抛），不做档位区分。
        self.identity(params).await?;
        let faces: Vec<Value> = self
            .registry
            .list()
            .into_iter()
            .map(|f| {
                json!({
                    "id": f.id,
                    "table": f.table,
                    "scope": f.scope.as_str(),
                    "tenant_column": f.tenant_column,
                })
            })
            .collect();
        Ok((200, json!({ "faces": faces })))
    }

    /// capability 调用入口（信封与 db-admin 同形：成功 {status, body}，
    /// 业务失败 {status, error: {code, message}}，code 用 DB_READ_* 同码）。
    pub async fn call(&self, method: &str, params: Value) -> Result<Value, McpError> {
        let result = match method {
            "query" => self.query(&params).await,
            "faces" => self.faces(&params).await,
            other => {
                return Err(McpError::Protocol {
                    message: format!("{NAMESPACE}.{other} not implemented (known: query, faces)"),
                });
            }
        };
        Ok(match result {
            Ok((status, body)) => json!({ "status": status, "body": body }),
            Err(e) => json!({
                "status": e.status,
                "error": { "code": e.code, "message": e.message },
            }),
        })
    }
}

/// 认证类 ApiError → HTTP 状态码（与 api_error_parts 同映射）。
fn api_status(e: &ApiError) -> u16 {
    match e {
        ApiError::BadRequest { .. } => 400,
        ApiError::Unauthorized { .. } => 401,
        ApiError::Forbidden { .. } => 403,
        ApiError::NotFound { .. } => 404,
        ApiError::Conflict { .. } => 409,
        ApiError::UnprocessableEntity { .. } => 422,
        ApiError::TooManyRequests { .. } => 429,
        ApiError::Internal { .. } | ApiError::WebSocket { .. } => 500,
        ApiError::ServiceUnavailable { .. } => 503,
    }
}

// ── 入口 2：HTTP 管理端点 POST /api/v1/db/read ───────────────────────

/// HTTP 端点运行态（boot 期装配； AppState 无 db-read 字段可挂，经全局单例
/// 桥接——pipeline_state_registry::global_registry 同款先例）。
pub struct DbReadHttpService {
    /// face 注册表。
    pub registry: Arc<ReadFacesRegistry>,
    /// 用户/租户解析用存储后端。
    pub store: Option<Arc<dyn agentos_core::traits::StorageBackend>>,
}

static HTTP_SERVICE: OnceLock<Arc<DbReadHttpService>> = OnceLock::new();

/// boot 期装配 HTTP 端点运行态（重复装配 = 装配错误，Err 拒绝）。
pub fn install_http_service(service: DbReadHttpService) -> Result<(), String> {
    HTTP_SERVICE
        .set(Arc::new(service))
        .map_err(|_| "db-read HTTP 服务重复装配".to_string())
}

/// boot 期一键装配（agentos-kernel.rs 调用）：face 注册表装载（fail-closed：
/// YAML 损坏/校验不过 = Err 拒绝启动；文件缺失 = 空注册表，对齐 storage.yaml
/// 缺失宽松先例）+ HTTP 端点服务安装 + capability 臂 handler 构造。
///
/// `sqlite_path` = None（非 SQLite driver）或 `":memory:"` 时返回 `Ok(None)`，
/// 两入口诚实降级（臂报未注入、HTTP 端点报 503）。
pub fn boot_setup(
    config_root: &Path,
    sqlite_path: Option<&str>,
    store: Option<Arc<dyn agentos_core::traits::StorageBackend>>,
) -> Result<Option<Arc<DbReadCapabilityHandler>>, String> {
    let Some(db_path) = sqlite_path.filter(|p| *p != ":memory:") else {
        return Ok(None);
    };
    let registry = Arc::new(ReadFacesRegistry::load(config_root, db_path)?);
    install_http_service(DbReadHttpService {
        registry: registry.clone(),
        store: store.clone(),
    })?;
    Ok(Some(Arc::new(DbReadCapabilityHandler::new(
        store, registry,
    ))))
}

/// `POST /api/v1/db/read`：body `{face, sql, params?, tenant?}`，出参
/// `{columns, rows}`；错误 `{error: {code, message}}` + 对应状态码。
///
/// 鉴权双层：/api/v1/ 前缀中间件（POST → admin）+ 本 handler 内
/// resolve_request_user 复权（纵深防御，db-admin capability.rs 同款）。
pub async fn db_read_http_handler(headers: HeaderMap, body: Bytes) -> Response {
    let Some(service) = HTTP_SERVICE.get() else {
        return DbReadError::storage(503, "db-read 服务未装配（内核启动期未注入）").into_response();
    };
    let parsed: Value = if body.is_empty() {
        Value::Null
    } else {
        match serde_json::from_slice(&body) {
            Ok(v) => v,
            Err(e) => {
                return DbReadError::sql_rejected(format!("body 非合法 JSON: {e}")).into_response()
            }
        }
    };
    match db_read_http_inner(service, &headers, &parsed).await {
        Ok(body) => (axum::http::StatusCode::OK, axum::Json(body)).into_response(),
        Err(e) => e.into_response(),
    }
}

impl IntoResponse for DbReadError {
    fn into_response(self) -> Response {
        let status = axum::http::StatusCode::from_u16(self.status)
            .unwrap_or(axum::http::StatusCode::INTERNAL_SERVER_ERROR);
        (
            status,
            axum::Json(json!({ "error": { "code": self.code, "message": self.message } })),
        )
            .into_response()
    }
}

/// HTTP 面共享执法路径（与 capability 面同一 [`query`]；拆出便于测试注入）。
async fn db_read_http_inner(
    service: &Arc<DbReadHttpService>,
    headers: &HeaderMap,
    body: &Value,
) -> Result<Value, DbReadError> {
    DbReadCapabilityHandler::reject_identity_params(body)?;
    let (_, _, role, tenant_id) = resolve_request_user(service.store.as_ref(), headers)
        .await
        .map_err(DbReadError::from_auth_error)?;
    let face = body
        .get("face")
        .and_then(|v| v.as_str())
        .ok_or_else(|| DbReadError::sql_rejected("缺少 face 参数"))?
        .to_string();
    let sql = body
        .get("sql")
        .and_then(|v| v.as_str())
        .ok_or_else(|| DbReadError::sql_rejected("缺少 sql 参数"))?
        .to_string();
    let bind_params = body.get("params").cloned().unwrap_or(Value::Null);
    let tenant_override = body
        .get("tenant")
        .and_then(|v| v.as_str())
        .map(str::to_string);
    let registry = Arc::clone(&service.registry);
    let result = spawn_blocking(move || {
        query(
            &registry,
            &face,
            &sql,
            &bind_params,
            &DbReadIdentity { tenant_id, role },
            tenant_override.as_deref(),
        )
    })
    .await
    .map_err(|e| DbReadError::storage(500, format!("数据库任务失败: {e}")))??;
    Ok(json!({ "columns": result.columns, "rows": result.rows }))
}

#[cfg(test)]
mod tests;
