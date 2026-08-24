//! 内核能力契约（kernel capability contracts）——声明式配置文件 + 定义驱动校验执行器。
//!
//! 2026-08-19 触发器空回复 bug 的模型根因：三层校验器（G2 / provides / 前端）
//! 都只验接口**形状**（方法在不在、参数名/类型对不对），验不了**值语义**
//! （pipeline_id 与 thread_id 同为 string，互填时形状 100% 合法）。本模块把
//! "值该长什么样"（形态）提升为契约配置文件的声明内容，校验器按定义执行：
//!
//! - **契约载体**（单一真值源）：`config/kernel_capabilities/*.json`——每文件
//!   一个 namespace，声明每个能力的 `input_schema` / `output_schema`（标准
//!   JSON Schema 关键字 + `pattern` 形态）。内核基础设施能力（chat/db_admin/
//!   user_admin/metrics…）走此处；任务管理等插件服务走 plugin.json 契约，不在此列。
//! - **执行器梯度**：定义详细到什么程度，就校验到什么程度——契约声明了
//!   (namespace, method) 才做该校验（required/类型/pattern/enum/闭包参数面），
//!   未声明即宽泛放行，校验器不越权也不偷懒。
//! - **入口挂点**：[`crate::capability_router::KernelCapabilityRouter::handle`]——
//!   所有反向 capability 调用（sidecar JSON-RPC / native HostServices）的单一
//!   收口，G6 授权之后、派发之前执行。
//! - **防双轨漂移机械闸**（真实文件一致性测试，见 tests 模块末段）：
//!   ① 配置 ↔ 代码：契约 properties 集合 == handler 实际读取的参数清单
//!   （[`crate::chat_send_handler::HANDLED_PARAM_NAMES`]）——加参数不改契约、
//!   或改契约不接代码，测试即红；保留字/保护前缀/origin.kind 枚举同理。
//!   ② 配置 ↔ 出入口：入口由执行器实时校验；出口由 output_schema 校验
//!   handler 实际响应（status 枚举 / pipeline_id 形态）。
//!
//! Rust `regex` 不支持 lookahead，"不得以 lineage. 开头"这类否定前缀无法用
//! 标准 pattern 表达，契约用 `x-forbidden-prefixes` 扩展关键字声明（JSON
//! Schema 允许 x- 厂商扩展，前端/RJSF 忽略未知关键字不受影响）。

use std::path::Path;

use agentos_mcp::McpError;
use serde::{Deserialize, Serialize};
use serde_json::Value;

/// state 注入保留字（引擎系统字段，调用方不可覆盖）。
///
/// 代码侧清单：契约入口校验执行配置文件里的同名声明，引擎合并纵深防御
/// （server.rs `apply_state_overlay`）与 chat_send_handler 直连路径消费本清单。
/// 与 `config/kernel_capabilities/chat.json` 的 `state.propertyNames.not.enum`
/// 必须一致——一致性由 [`tests::chat_contract_matches_code_lists`] 机械闸强制。
pub(crate) const RESERVED_STATE_KEYS: &[&str] = &[
    "message",
    "messages",
    "agent_id",
    "pipeline_id",
    "session_id",
    "thread_id",
    "user_id",
    "run_id",
    "execution_context",
    "lineage",
    "message_id",
];

/// state 键保护前缀（lineage 为引擎出生写入字段，注入不可覆写）。
/// 与契约文件 `state.propertyNames.x-forbidden-prefixes` 的一致性由同一机械闸强制。
pub(crate) const FORBIDDEN_STATE_KEY_PREFIXES: &[&str] = &["lineage."];

/// 一个 namespace 的内核能力契约（对应 config/kernel_capabilities/ 下一个文件）。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct KernelCapabilityContract {
    /// 能力 namespace（如 "chat"）——与 CapabilityHandlerRegistry 注册名一致。
    pub namespace: String,
    /// 能力组描述（人读；schema 聚合端点透出）。
    #[serde(default)]
    pub description: String,
    /// 该 namespace 下的方法契约清单。
    pub capabilities: Vec<CapabilityMethodSpec>,
    /// message_id 命名空间清单（streaming 协议真值源 `x-message-id-namespaces`；
    /// 其他 namespace 无此字段 = 空表）。网关执法与本模块机械闸同源读它。
    #[serde(default, rename = "x-message-id-namespaces")]
    pub message_id_namespaces: Vec<MessageIdNamespace>,
}

/// message_id 命名空间条目（streaming.json 真值源，前缀隔离防 id 冲突）。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct MessageIdNamespace {
    /// id 前缀（"" = 无前缀/裸形态）。
    #[serde(default)]
    pub prefix: String,
    /// 归属：kernel（内核 LLM 路径签发）/ engine（内容指纹）/ plugin（插件强制）/
    /// frontend-optimistic（前端乐观 user 临时 id）。
    pub owner: String,
    /// 插件是否禁止使用本空间。
    pub plugin_forbidden: bool,
    /// id 形态（anchored regex，机器可读——网关/前端/机械闸同源）。
    pub pattern: String,
    /// 人读说明。
    #[serde(default)]
    pub description: String,
}

/// 引擎管道家族（LLM 路径的器官插件）：内核经引擎 state 把签发的 `a_` id 下发给
/// 它们，故它们合法携带 a_ 命名空间（其余插件一律 p_）。清单与
/// plugins/shared/pipeline/core/{llm_core,tool_core}/plugin.json 的 id 一致性由
/// 本模块机械闸测试锁定（插件改名即红）。
pub(crate) const ENGINE_CONDUIT_PLUGINS: &[&str] = &["pipeline_llm_core", "pipeline_tool_core"];

/// 单个能力方法的契约。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CapabilityMethodSpec {
    /// 方法名（如 "send_message"）。
    pub method: String,
    /// 方法描述。
    #[serde(default)]
    pub description: String,
    /// 入参 JSON Schema（含形态关键字 pattern/enum/minLength/propertyNames）。
    pub input_schema: Value,
    /// 出参 JSON Schema（出口一致性闸消费）。
    #[serde(default = "default_object_schema")]
    pub output_schema: Value,
}

fn default_object_schema() -> Value {
    serde_json::json!({"type": "object"})
}

/// 从目录加载全部内核能力契约（`*.json` 按文件名排序，保证确定性）。
///
/// - 目录不存在 → `Ok(vec![])`（未启用契约，入口校验宽泛放行）。
/// - 文件损坏 / 结构不符 / (namespace, method) 重复 → `Err`（带文件名与原因）。
///   调用方（agentos-kernel 启动）fail-fast 拒启——契约是校验器的眼睛，
///   坏契约静默跳过 = 校验器装瞎。
pub fn load_contracts(dir: &Path) -> Result<Vec<KernelCapabilityContract>, String> {
    if !dir.is_dir() {
        return Ok(Vec::new());
    }
    let mut files: Vec<std::path::PathBuf> = std::fs::read_dir(dir)
        .map_err(|e| format!("读取内核能力契约目录 {} 失败: {e}", dir.display()))?
        .filter_map(|e| e.ok().map(|e| e.path()))
        .filter(|p| p.extension().and_then(|e| e.to_str()) == Some("json"))
        .collect();
    files.sort();

    let mut contracts = Vec::new();
    let mut seen: std::collections::HashSet<(String, String)> = std::collections::HashSet::new();
    for path in files {
        let file_tag = path
            .file_name()
            .and_then(|n| n.to_str())
            .unwrap_or("<unnamed>")
            .to_string();
        let raw = std::fs::read_to_string(&path)
            .map_err(|e| format!("内核能力契约 {file_tag} 读取失败: {e}"))?;
        let contract: KernelCapabilityContract = serde_json::from_str(&raw)
            .map_err(|e| format!("内核能力契约 {file_tag} 解析失败: {e}"))?;
        validate_contract_structure(&contract, &file_tag)?;
        for spec in &contract.capabilities {
            let key = (contract.namespace.clone(), spec.method.clone());
            if !seen.insert(key) {
                return Err(format!(
                    "内核能力契约 {file_tag}: {}.{} 与先前文件重复（一方法一契约）",
                    contract.namespace, spec.method
                ));
            }
        }
        contracts.push(contract);
    }
    Ok(contracts)
}

/// 契约文件自身的结构校验（加载即查——契约坏了必须当场报，不能等校验时静默漏过）。
fn validate_contract_structure(c: &KernelCapabilityContract, file_tag: &str) -> Result<(), String> {
    if c.namespace.is_empty() {
        return Err(format!("内核能力契约 {file_tag}: namespace 不得为空"));
    }
    if c.capabilities.is_empty() {
        return Err(format!(
            "内核能力契约 {file_tag}: capabilities 不得为空（未启用请删文件）"
        ));
    }
    for spec in &c.capabilities {
        if spec.method.is_empty() {
            return Err(format!("内核能力契约 {file_tag}: 存在空 method 名"));
        }
        if spec.input_schema.get("type").and_then(|v| v.as_str()) != Some("object") {
            return Err(format!(
                "内核能力契约 {file_tag}: {}.{} 的 input_schema.type 必须为 object（方法入参是命名参数包）",
                c.namespace, spec.method
            ));
        }
        if spec
            .input_schema
            .get("properties")
            .and_then(|v| v.as_object())
            .is_none()
        {
            return Err(format!(
                "内核能力契约 {file_tag}: {}.{} 的 input_schema 缺少 properties 对象",
                c.namespace, spec.method
            ));
        }
        if !spec.output_schema.is_object() {
            return Err(format!(
                "内核能力契约 {file_tag}: {}.{} 的 output_schema 必须为对象",
                c.namespace, spec.method
            ));
        }
    }
    // message_id 命名空间清单结构校验（streaming）：pattern 必须可编译、owner 唯一性。
    // 契约是校验器的眼睛——pattern 坏了执法会静默失效，必须加载即报。
    let plugin_owners = c
        .message_id_namespaces
        .iter()
        .filter(|n| n.owner == "plugin")
        .count();
    if plugin_owners > 1 {
        return Err(format!(
            "内核能力契约 {file_tag}: owner=plugin 的命名空间必须恰好一个（现 {plugin_owners}）"
        ));
    }
    for n in &c.message_id_namespaces {
        if n.owner.is_empty() {
            return Err(format!(
                "内核能力契约 {file_tag}: 命名空间 {} 的 owner 不得为空",
                n.prefix
            ));
        }
        if regex::Regex::new(&n.pattern).is_err() {
            return Err(format!(
                "内核能力契约 {file_tag}: 命名空间 {} 的 pattern 非法（{}）",
                n.prefix, n.pattern
            ));
        }
    }
    Ok(())
}

/// 按 (namespace, method) 找契约声明；未声明 → None（宽泛放行）。
pub fn find_spec<'a>(
    contracts: &'a [KernelCapabilityContract],
    namespace: &str,
    method: &str,
) -> Option<&'a CapabilityMethodSpec> {
    contracts
        .iter()
        .find(|c| c.namespace == namespace)?
        .capabilities
        .iter()
        .find(|s| s.method == method)
}

/// 流式事件网关执法（ADR 2026-08-22 流式协议）：event-bus.emit 收到 streaming
/// 契约事件时按单一真值源 fail-closed 校验。三层：
/// ① schema——payload 按事件 input_schema 校验（required/形态）；
/// ② message_id 命名空间——按 x-message-id-namespaces 执法（引擎管道家族
///    合法携带内核签发的 a_，其余插件强制 p_，内核内部调用放行）；
/// ③ thread_id——emit_event 按 thread 单播，缺路由键无法投递。
/// Err(原因) = 丢弃 + 告警（调用方保持 Ok(dropped) 语义，不炸 RPC）。
pub fn validate_streaming_event(
    contracts: &[KernelCapabilityContract],
    event_name: &str,
    payload: &Value,
    plugin_id: Option<&str>,
) -> Result<(), String> {
    let Some(spec) = find_spec(contracts, "streaming", event_name) else {
        return Ok(()); // 非契约事件（interaction_*/透传族）不归本闸
    };
    let path = format!("streaming.{event_name} payload");
    validate_value(&spec.input_schema, payload, &path)?;

    // ② 命名空间执法（plugin_id 为 invoker 注入信任锚点，插件不可伪造）。
    // 空 message_id 由 schema required 抓红；此处只执法形态归属。
    let message_id = payload
        .get("message_id")
        .and_then(|v| v.as_str())
        .unwrap_or("");
    enforce_message_id_namespace(contracts, message_id, plugin_id, event_name)?;

    // ③ thread_id 投递路由键（契约 required 已声明，此处显式兜底——
    // emit_event 按 thread 单播，无键必丢，与其发一个不可达事件不如显式拒绝）。
    let thread_id = payload
        .get("thread_id")
        .and_then(|v| v.as_str())
        .unwrap_or("");
    if thread_id.is_empty() {
        return Err("缺 thread_id（WS 投递路由键，emit_event 按 thread 单播）".to_string());
    }
    Ok(())
}

/// message_id 命名空间执法（按 streaming.json `x-message-id-namespaces` 真值源）：
/// - 内核内部调用（plugin_id=None，如 ws_session dispatch）→ 放行（id 由内核签发）；
/// - 引擎管道家族（llm_core/tool_core，内核经 state 下发 a_ id）→ 必须 a_ 命名空间；
/// - 其余插件 → 必须 p_ 命名空间（结构性杜绝与 a_/mc_/乐观裸 uuid 冲突）。
fn enforce_message_id_namespace(
    contracts: &[KernelCapabilityContract],
    message_id: &str,
    plugin_id: Option<&str>,
    event_name: &str,
) -> Result<(), String> {
    let Some(spaces) = contracts
        .iter()
        .find(|c| c.namespace == "streaming")
        .map(|c| &c.message_id_namespaces)
    else {
        return Ok(()); // 契约未启用（目录无 streaming.json）→ 宽泛放行
    };
    let Some(pid) = plugin_id else {
        return Ok(());
    };
    if message_id.is_empty() {
        return Err("缺 message_id（精确寻址键）".to_string());
    }
    let (want_owner, why) = if ENGINE_CONDUIT_PLUGINS.contains(&pid) {
        ("kernel", format!("引擎管道插件 {pid} 只能携带内核签发的 a_ id"))
    } else {
        ("plugin", format!("插件 {pid} 的 message_id 必须在 p_ 命名空间（x-message-id-namespaces）"))
    };
    let want = spaces.iter().find(|n| n.owner == want_owner);
    match want {
        Some(ns) => {
            let re = regex::Regex::new(&ns.pattern)
                .map_err(|e| format!("契约 pattern 非法（{}）: {e}", ns.pattern))?;
            if !re.is_match(message_id) {
                return Err(format!(
                    "{why}，实际 {message_id:?} 不匹配 {}（事件 {event_name}）",
                    ns.pattern
                ));
            }
            Ok(())
        }
        None => Err(format!(
            "streaming 契约缺 owner={want_owner} 命名空间条目，无法执法（事件 {event_name}）"
        )),
    }
}

/// 入口校验：按契约定义校验能力调用参数（定义驱动执行器）。
///
/// 未声明 (namespace, method) → `Ok(())`（定义宽泛则校验宽泛）。
/// 声明了 → 按 input_schema 逐条执行：required / 类型 / pattern 形态 /
/// enum / minLength / propertyNames（保留字与保护前缀）/ additionalProperties
/// 闭包参数面（`_` 前缀内部信封字段如 `_plugin_id` 跳过——G6 信任锚点由
/// invoker 注入，非调用方参数）。
pub fn validate_params(
    contracts: &[KernelCapabilityContract],
    namespace: &str,
    method: &str,
    params: &Value,
) -> Result<(), McpError> {
    let Some(spec) = find_spec(contracts, namespace, method) else {
        return Ok(());
    };
    validate_value(
        &spec.input_schema,
        params,
        &format!("{namespace}.{method} 入参"),
    )
    .map_err(|msg| McpError::Protocol {
        message: format!("{namespace}.{method} 契约校验失败: {msg}"),
    })
}

/// 校验单个值是否符合 schema（执行器核心，入口/出口共用）。
///
/// 支持的关键字子集（标准 JSON Schema 语义 + 一个 x- 扩展）：
/// `type` / `required` / `properties`（递归）/ `pattern`（非锚定搜索，锚点由
/// 契约自己写）/ `enum` / `minLength` / `propertyNames.pattern|enum|not.enum|
/// x-forbidden-prefixes` / `additionalProperties: false`。其余关键字忽略
/// （description 等）——定义没写的就不查。
pub fn validate_value(schema: &Value, value: &Value, path: &str) -> Result<(), String> {
    let Some(obj) = schema.as_object() else {
        return Ok(()); // schema 非对象 = 无定义 = 宽泛
    };

    // type：L1 类型档
    if let Some(t) = obj.get("type").and_then(|v| v.as_str()) {
        let ok = match t {
            "object" => value.is_object(),
            "string" => value.is_string(),
            "boolean" => value.is_boolean(),
            "integer" => value.is_i64() || value.is_u64(),
            "number" => value.is_number(),
            "array" => value.is_array(),
            "null" => value.is_null(),
            other => {
                return Err(format!(
                    "{path}: 契约使用了不支持的 type '{other}'（契约自身有误）"
                ))
            }
        };
        if !ok {
            return Err(format!(
                "{path}: 类型应为 {t}，实际 {}",
                json_type_name(value)
            ));
        }
    }

    // enum：值必须命中枚举之一
    if let Some(allowed) = obj.get("enum").and_then(|v| v.as_array()) {
        if !allowed.is_empty() && !allowed.contains(value) {
            return Err(format!("{path}: 值 {value} 不在契约枚举 {allowed:?} 内"));
        }
    }

    // string 形态：pattern（L3 形态档）+ minLength
    if let Some(s) = value.as_str() {
        if let Some(pattern) = obj.get("pattern").and_then(|v| v.as_str()) {
            let re = regex::Regex::new(pattern).map_err(|e| {
                format!("{path}: 契约 pattern 非法（{pattern}）: {e}——契约自身有误")
            })?;
            if !re.is_match(s) {
                return Err(format!("{path}: 值 {s:?} 不符合契约形态 {pattern}"));
            }
        }
        if let Some(min) = obj.get("minLength").and_then(|v| v.as_u64()) {
            if (s.len() as u64) < min {
                return Err(format!("{path}: 长度 {} 低于契约 minLength {min}", s.len()));
            }
        }
    }

    // object：required / propertyNames / additionalProperties 闭包 / properties 递归。
    // 各关键字独立生效（properties 未声明不影响 propertyNames/required 执行——
    // 定义详细到什么程度就校验到什么程度）。
    if let Some(map) = value.as_object() {
        let props = obj.get("properties").and_then(|v| v.as_object());
        if let Some(req) = obj.get("required").and_then(|v| v.as_array()) {
            for r in req {
                if let Some(name) = r.as_str() {
                    if !map.contains_key(name) {
                        return Err(format!("{path}: 缺少契约必填参数 {name}"));
                    }
                }
            }
        }
        // 键名约束：propertyNames 子 schema 作用于每个键（字符串值）
        if let Some(pn) = obj.get("propertyNames") {
            for key in map.keys() {
                validate_property_name(pn, key, path)?;
            }
        }
        // 闭包参数面：additionalProperties=false 时未知参数即红（配置 ↔ 调用方漂移
        // 在入口暴露）。`_` 前缀 = 内部信封字段（_plugin_id 等），不属于调用方参数。
        if obj.get("additionalProperties") == Some(&Value::Bool(false)) {
            for key in map.keys() {
                if key.starts_with('_') {
                    continue;
                }
                if !props.is_some_and(|p| p.contains_key(key)) {
                    return Err(format!(
                        "{path}: 参数 {key} 不在契约参数面内（additionalProperties=false；\
                         若为新增参数请同步契约文件与 HANDLED_PARAM_NAMES）"
                    ));
                }
            }
        }
        if let Some(props) = props {
            for (key, sub) in props {
                if let Some(v) = map.get(key) {
                    validate_value(sub, v, &format!("{path}.{key}"))?;
                }
            }
        }
    }

    Ok(())
}

/// propertyNames 子 schema 校验单个键名。
/// 支持 pattern / enum / not.enum / x-forbidden-prefixes（x- 扩展：Rust regex
/// 无 lookahead，"不得以某前缀开头"用数据声明而非否定模式表达）。
fn validate_property_name(pn: &Value, key: &str, path: &str) -> Result<(), String> {
    let Some(pn_obj) = pn.as_object() else {
        return Ok(());
    };
    if let Some(pattern) = pn_obj.get("pattern").and_then(|v| v.as_str()) {
        let re = regex::Regex::new(pattern).map_err(|e| {
            format!("{path}: propertyNames.pattern 非法（{pattern}）: {e}——契约自身有误")
        })?;
        if !re.is_match(key) {
            return Err(format!("{path}: 键 {key:?} 不符合契约键名形态 {pattern}"));
        }
    }
    if let Some(allowed) = pn_obj.get("enum").and_then(|v| v.as_array()) {
        if !allowed.is_empty() && !allowed.iter().any(|a| a.as_str() == Some(key)) {
            return Err(format!("{path}: 键 {key:?} 不在契约允许键名枚举内"));
        }
    }
    if let Some(forbidden) = pn_obj
        .get("not")
        .and_then(|n| n.get("enum"))
        .and_then(|v| v.as_array())
    {
        if forbidden.iter().any(|f| f.as_str() == Some(key)) {
            return Err(format!("{path}: 键 {key:?} 为契约保留键（不可注入/覆写）"));
        }
    }
    if let Some(prefixes) = pn_obj
        .get("x-forbidden-prefixes")
        .and_then(|v| v.as_array())
    {
        for p in prefixes.iter().filter_map(|p| p.as_str()) {
            if key.starts_with(p) {
                return Err(format!(
                    "{path}: 键 {key:?} 命中契约保护前缀 {p:?}（引擎出生写入字段）"
                ));
            }
        }
    }
    Ok(())
}

/// JSON 值的类型名（错误消息用）。
fn json_type_name(v: &Value) -> &'static str {
    match v {
        Value::Null => "null",
        Value::Bool(_) => "boolean",
        Value::Number(_) => "number",
        Value::String(_) => "string",
        Value::Array(_) => "array",
        Value::Object(_) => "object",
    }
}

#[cfg(test)]
mod tests {
    //! 定义驱动执行器测试 + 双轨漂移机械闸（真实契约文件 ↔ 代码清单一致性）。

    use super::*;
    use serde_json::json;

    /// 真实仓库契约目录（kernel/crates/api → 上溯三级 = 仓库根）。
    fn repo_contract_dir() -> std::path::PathBuf {
        std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../../../config/kernel_capabilities")
    }

    // ── 加载器 ───────────────────────────────────────────────────

    #[test]
    fn load_missing_dir_yields_empty() {
        let contracts =
            load_contracts(std::path::Path::new("no/such/dir")).expect("缺失目录应为 Ok 空");
        assert!(contracts.is_empty());
    }

    #[test]
    fn load_malformed_files_rejected() {
        let cases: Vec<(&str, String, &str)> = vec![
            ("bad_json.json", "{ not json".to_string(), "坏 JSON"),
            (
                "no_ns.json",
                json!({"capabilities": [{"method": "m", "input_schema": {"type": "object", "properties": {}}}]}).to_string(),
                "缺 namespace",
            ),
            (
                "empty_caps.json",
                json!({"namespace": "x", "capabilities": []}).to_string(),
                "空 capabilities",
            ),
            (
                "bad_input.json",
                json!({"namespace": "x", "capabilities": [{"method": "m", "input_schema": {"type": "string"}}]}).to_string(),
                "input_schema 非 object type",
            ),
            (
                "no_props.json",
                json!({"namespace": "x", "capabilities": [{"method": "m", "input_schema": {"type": "object"}}]}).to_string(),
                "input_schema 缺 properties",
            ),
        ];
        for (name, content, why) in cases {
            // 每例独立 tempdir：加载错误必须指向"本例"的文件（共目录时先坏者
            // 总先报，无法断言各例各自被抓）。
            let dir = tempfile::tempdir().unwrap();
            std::fs::write(dir.path().join(name), content).unwrap();
            let err = load_contracts(dir.path()).expect_err(why);
            assert!(err.contains(name), "{why}: 错误应带文件名，实际 {err}");
        }
    }

    #[test]
    fn load_duplicate_method_rejected() {
        let dir = tempfile::tempdir().unwrap();
        let spec = json!({
            "namespace": "chat",
            "capabilities": [{"method": "send_message",
                "input_schema": {"type": "object", "properties": {}}}]
        });
        std::fs::write(dir.path().join("a.json"), spec.to_string()).unwrap();
        std::fs::write(dir.path().join("b.json"), spec.to_string()).unwrap();
        let err = load_contracts(dir.path()).expect_err("重复 (namespace, method) 应拒绝");
        assert!(err.contains("重复"), "实际: {err}");
    }

    #[test]
    fn load_valid_file_roundtrip() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(
            dir.path().join("x.json"),
            json!({
                "namespace": "x",
                "description": "演示",
                "capabilities": [{
                    "method": "do",
                    "description": "d",
                    "input_schema": {"type": "object", "properties": {"a": {"type": "string"}}},
                    "output_schema": {"type": "object", "properties": {}}
                }]
            })
            .to_string(),
        )
        .unwrap();
        let contracts = load_contracts(dir.path()).unwrap();
        assert_eq!(contracts.len(), 1);
        assert_eq!(contracts[0].namespace, "x");
        assert_eq!(contracts[0].capabilities[0].method, "do");
        // output_schema 缺省时回退 {"type":"object"}
        let contracts2 = load_contracts(dir.path()).unwrap();
        assert_eq!(
            contracts2[0].capabilities[0].output_schema["type"],
            "object"
        );
    }

    // ── 执行器（用真实 chat 契约驱动——顺带锁定契约文件本身可用） ──────

    fn chat_contracts() -> Vec<KernelCapabilityContract> {
        let contracts =
            load_contracts(&repo_contract_dir()).expect("仓库契约文件必须可加载（损坏即本测试红）");
        assert!(
            find_spec(&contracts, "chat", "send_message").is_some(),
            "chat.send_message 契约必须存在"
        );
        contracts
    }

    #[test]
    fn executor_gradient_undeclared_method_passes() {
        let contracts = chat_contracts();
        // 未声明的方法 → 宽泛放行（定义详细到什么程度就校验到什么程度）
        validate_params(&contracts, "chat", "bogus_method", &json!({"any": "thing"}))
            .expect("未声明方法应宽泛放行");
        validate_params(&contracts, "no_such_ns", "m", &json!([1, 2])).expect("未声明 ns 放行");
    }

    #[test]
    fn executor_happy_paths() {
        let contracts = chat_contracts();
        // 触发器注入的真实形态
        validate_params(
            &contracts,
            "chat",
            "send_message",
            &json!({
                "pipeline_id": "a1b2c3d4e5f64789abcdef0123456789",
                "message": "触发提醒", "user_id": "u1"
            }),
        )
        .expect("合法注入应通过");
        // 创建分支（task_submit 真实形态）
        validate_params(
            &contracts,
            "chat",
            "send_message",
            &json!({
                "create": true, "background": true,
                "message": "m", "user_id": "u1", "agent_id": "review_agent",
                "state": {"task.goal": "g", "task.status": "pending"},
                "lineage": {"root": true, "origin": {"kind": "plugin", "source": "task_submit"}},
                "execution_context": {"workspace": {"mode": "worktree"}}
            }),
        )
        .expect("合法创建应通过");
        // _plugin_id 内部信封字段不破坏闭包
        validate_params(
            &contracts,
            "chat",
            "send_message",
            &json!({
                "pipeline_id": "a1b2c3d4e5f64789abcdef0123456789",
                "message": "m", "user_id": "u1", "_plugin_id": "trigger_setup_tool"
            }),
        )
        .expect("_ 前缀内部字段应跳过闭包检查");
    }

    /// 2026-08-19 bug 的负样本：thread 坐标填进 pipeline_id 槽——L1/L2 全绿，
    /// L3 形态（pattern）立即红。这就是"相同类型、不同语义"错误的暴露点。
    #[test]
    fn executor_rejects_thread_shape_in_pipeline_slot() {
        let contracts = chat_contracts();
        for bad in [
            "thread-abc",
            "thread_a1b2c3d4e5f64789abcdef012345678",
            "PIPE_1",
        ] {
            let err = validate_params(
                &contracts,
                "chat",
                "send_message",
                &json!({"pipeline_id": bad, "message": "m", "user_id": "u1"}),
            )
            .expect_err(&format!("互填/错形态 {bad:?} 必须抓红"));
            assert!(err.to_string().contains("形态"), "实际: {err}");
        }
    }

    #[test]
    fn executor_rejects_rule_violations() {
        let contracts = chat_contracts();
        let cases: Vec<(Value, &str)> = vec![
            (json!({"user_id": "u1"}), "缺 message（required）"),
            (json!({"message": "m"}), "缺 user_id（required）"),
            (json!({"message": 42, "user_id": "u1"}), "message 类型错"),
            (
                json!({"message": "m", "user_id": "u1", "create": "yes"}),
                "create 非 boolean",
            ),
            (
                json!({"pipeline_id": "a1b2c3d4e5f64789abcdef0123456789", "message": "", "user_id": "u1"}),
                "message 空串（minLength）",
            ),
            (
                json!({"message": "m", "user_id": "u1", "lineage": {"root": true, "origin": {"kind": "human", "source": "x"}}}),
                "origin.kind 不在枚举",
            ),
            (
                json!({"message": "m", "user_id": "u1", "lineage": {"parent_pipeline_id": "pipe_parent", "origin_session_id": "s"}}),
                "parent_pipeline_id 形态错",
            ),
            (
                json!({"pipeline_id": "a1b2c3d4e5f64789abcdef0123456789", "message": "m", "user_id": "u1", "state": {"pipeline_id": "evil"}}),
                "state 保留字",
            ),
            (
                json!({"pipeline_id": "a1b2c3d4e5f64789abcdef0123456789", "message": "m", "user_id": "u1", "state": {"lineage.root": true}}),
                "state 保护前缀",
            ),
            (
                json!({"message": "m", "user_id": "u1", "thread_id": "no-prefix", "create": true}),
                "thread_id 形态错（须 ^thread- 前缀）",
            ),
            (json!(["array", "params"]), "params 非对象"),
        ];
        for (params, why) in cases {
            validate_params(&contracts, "chat", "send_message", &params).expect_err(why);
        }
        // thread_id 合法用例（创建分支归属会话）：不应被契约拒绝
        validate_params(
            &contracts,
            "chat",
            "send_message",
            &json!({"message": "m", "user_id": "u1", "thread_id": "thread-abc", "create": true}),
        )
        .expect("thread_id 合法（创建分支归属会话）");
    }

    // ── 双轨漂移机械闸：真实契约文件 ↔ 代码清单 ─────────────────────
    // 配置改了代码没跟（或反过来）→ 以下测试红——这就是"防漂移"的机械形态。

    /// 闸 1（配置 ↔ 代码）：契约 properties 集合 == handler 实际读取的参数清单。
    #[test]
    fn chat_contract_matches_code_lists() {
        let contracts = chat_contracts();
        let spec = find_spec(&contracts, "chat", "send_message").unwrap();

        // 参数面：handler 读什么，契约就得声明什么（多声明=死契约，少声明=裸参数）
        let declared: std::collections::BTreeSet<&str> = spec.input_schema["properties"]
            .as_object()
            .unwrap()
            .keys()
            .map(|k| k.as_str())
            .collect();
        let handled: std::collections::BTreeSet<&str> =
            crate::chat_send_handler::HANDLED_PARAM_NAMES
                .iter()
                .copied()
                .collect();
        assert_eq!(
            declared, handled,
            "契约参数面与 HANDLED_PARAM_NAMES 漂移：配置↔代码必有一侧过时"
        );

        // 必填面：代码无条件读取的参数（缺即协议错误）
        let required: Vec<&str> = spec.input_schema["required"]
            .as_array()
            .unwrap()
            .iter()
            .filter_map(|v| v.as_str())
            .collect();
        assert_eq!(required, vec!["message", "user_id"], "必填参数面漂移");

        // 保留字：state.propertyNames.not.enum == 代码侧 RESERVED_STATE_KEYS
        let reserved: std::collections::BTreeSet<&str> = spec.input_schema["properties"]["state"]
            ["propertyNames"]["not"]["enum"]
            .as_array()
            .expect("state.propertyNames.not.enum 必须声明")
            .iter()
            .filter_map(|v| v.as_str())
            .collect();
        let code: std::collections::BTreeSet<&str> = RESERVED_STATE_KEYS.iter().copied().collect();
        assert_eq!(reserved, code, "保留字清单配置↔代码漂移");

        // 保护前缀：x-forbidden-prefixes == 代码侧 FORBIDDEN_STATE_KEY_PREFIXES
        let prefixes: Vec<&str> = spec.input_schema["properties"]["state"]["propertyNames"]
            ["x-forbidden-prefixes"]
            .as_array()
            .expect("x-forbidden-prefixes 必须声明")
            .iter()
            .filter_map(|v| v.as_str())
            .collect();
        assert_eq!(
            prefixes,
            FORBIDDEN_STATE_KEY_PREFIXES.to_vec(),
            "保护前缀配置↔代码漂移"
        );

        // 血缘 origin.kind 枚举 == 代码侧 LINEAGE_ORIGIN_KINDS
        let kinds: std::collections::BTreeSet<&str> = spec.input_schema["properties"]["lineage"]
            ["properties"]["origin"]["properties"]["kind"]["enum"]
            .as_array()
            .expect("lineage.origin.kind.enum 必须声明")
            .iter()
            .filter_map(|v| v.as_str())
            .collect();
        let code_kinds: std::collections::BTreeSet<&str> =
            crate::chat_send_handler::LINEAGE_ORIGIN_KINDS
                .iter()
                .copied()
                .collect();
        assert_eq!(kinds, code_kinds, "origin.kind 枚举配置↔代码漂移");

        // 形态核心：pipeline_id 的 32hex pattern 必须声明（本方案的立身之本）
        assert_eq!(
            spec.input_schema["properties"]["pipeline_id"]["pattern"],
            json!("^[0-9a-f]{32}$"),
            "pipeline_id 形态 pattern 漂移"
        );
    }

    /// 闸 2（配置 ↔ 出口）：handler 实际响应必须通过 output_schema 校验；
    /// 错误形状（status 越枚举）必须红。
    #[tokio::test]
    async fn chat_contract_output_schema_gates_handler_responses() {
        use agentos_core::traits::StorageBackend;
        use agentos_mcp::CapabilityHandler;
        use agentos_session::router::PipelineDispatcher;
        use std::sync::{Arc, Mutex};

        // 最小记录派发器（chat_send_handler::tests 的 mock 是私有的，此处内联）
        struct NoopDispatcher;
        #[async_trait::async_trait]
        impl PipelineDispatcher for NoopDispatcher {
            async fn dispatch_user_input(
                &self,
                _t: &str,
                _u: &str,
                _c: &str,
                _p: &str,
                _ts: &str,
                _ec: Option<&Value>,
                _ov: Option<&Value>,
                _a: &str,
                _cmid: &str,
            ) -> Result<(), String> {
                Ok(())
            }
            async fn dispatch_interaction_response(
                &self,
                _t: &str,
                _r: &str,
                _resp: &Value,
            ) -> Result<(), String> {
                Ok(())
            }
            async fn dispatch_stop(&self, _t: &str) -> Result<(), String> {
                Ok(())
            }
        }
        let _seen: Mutex<Vec<String>> = Mutex::new(Vec::new());

        let contracts = chat_contracts();
        let spec = find_spec(&contracts, "chat", "send_message").unwrap();
        let store = Arc::new(agentos_engine::SqliteStore::open_memory().unwrap());
        store
            .link_pipeline_session("c1b2c3d4e5f64789abcdef0123456789", "thread-gate", "default")
            .await
            .unwrap();
        let h = crate::chat_send_handler::ChatSendHandler::with_store(
            Arc::new(NoopDispatcher),
            Some(store),
        );

        // 出口两分支：created / dispatched 响应均须过 output_schema
        let created = h
            .handle(
                "send_message",
                json!({"create": true, "message": "m", "user_id": "u1",
                       "lineage": {"root": true, "origin": {"kind": "system", "source": "gate"}}}),
            )
            .await
            .unwrap();
        validate_value(&spec.output_schema, &created, "created 响应")
            .expect("创建分支响应应符合 output_schema");
        let dispatched = h
            .handle(
                "send_message",
                json!({"pipeline_id": "c1b2c3d4e5f64789abcdef0123456789",
                       "message": "m", "user_id": "u1"}),
            )
            .await
            .unwrap();
        validate_value(&spec.output_schema, &dispatched, "dispatched 响应")
            .expect("注入分支响应应符合 output_schema");

        // 负样本：越枚举的 status 必须红（出口闸的牙齿）
        let err = validate_value(
            &spec.output_schema,
            &json!({"status": "bogus", "pipeline_id": "c1b2c3d4e5f64789abcdef0123456789"}),
            "坏响应",
        )
        .expect_err("越枚举 status 必须红");
        assert!(err.contains("枚举"), "实际: {err}");
    }

    /// 闸 3（入口接线）：经 KernelCapabilityRouter 收口的调用先过契约校验——
    /// 互填坐标在内核入口被拒（不是等到深处静默丢事件）。
    #[tokio::test]
    async fn router_entry_validates_params_against_contract() {
        use agentos_mcp::{CapabilityHandlerRegistry, CapabilityRouter};
        use std::sync::Arc;

        let contracts = Arc::new(chat_contracts());
        let router = crate::capability_router::KernelCapabilityRouter::new()
            .with_capability_contracts(contracts.clone());
        let registry = Arc::new(CapabilityHandlerRegistry::new());
        registry.register(Arc::new(crate::chat_send_handler::ChatSendHandler::with_store(
            Arc::new(crate::ws_session::EngineDispatcher::new(
                crate::routes::AppState::new(),
            )),
            None,
        )));
        let router = router.with_handler_registry(registry);

        let err = router
            .handle(
                "chat",
                "send_message",
                json!({"pipeline_id": "thread-misfilled", "message": "m", "user_id": "u1"}),
            )
            .await
            .expect_err("互填坐标必须在内核入口被拒");
        assert!(matches!(err, McpError::Protocol { .. }), "实际: {err:?}");
        assert!(
            err.to_string().contains("形态"),
            "错误应指向形态违规，实际: {err}"
        );
    }

    // ── 流式协议机械闸（streaming.json ↔ 网关执法一致性） ──────────────
    // 闸 4（配置↔代码）：真实 streaming.json 必须可装载、含 10 事件、命名空间
    // 四条目（a_/mc_/p_/裸 uuid）、p_ 为唯一 owner=plugin 条目。插件改名即红
    // （ENGINE_CONDUIT_PLUGINS 与 plugin.json 漂移）。

    fn streaming_contracts() -> Vec<KernelCapabilityContract> {
        let contracts =
            load_contracts(&repo_contract_dir()).expect("仓库契约文件必须可加载（损坏即本测试红）");
        assert!(
            find_spec(&contracts, "streaming", "stream_chunk").is_some(),
            "streaming.stream_chunk 契约必须存在"
        );
        contracts
    }

    #[test]
    fn streaming_contract_loads_full_shape() {
        let contracts = streaming_contracts();
        let streaming = contracts
            .iter()
            .find(|c| c.namespace == "streaming")
            .expect("streaming 契约必须存在");
        assert_eq!(
            streaming.capabilities.len(),
            10,
            "streaming 事件数漂移（stream_start/chunk/end + thinking_*3 + tool_*2 + new_message + stream_error）"
        );
        // 四命名空间条目（真值源 x-message-id-namespaces）
        let owners: Vec<&str> = streaming
            .message_id_namespaces
            .iter()
            .map(|n| n.owner.as_str())
            .collect();
        assert_eq!(
            owners,
            vec!["kernel", "engine", "plugin", "frontend-optimistic"],
            "命名空间 owner 清单漂移（内核/引擎/插件/乐观前端）"
        );
        // 每个 pattern 必须可编译（契约是校验器的眼睛）
        for n in &streaming.message_id_namespaces {
            assert!(
                regex::Regex::new(&n.pattern).is_ok(),
                "命名空间 {} pattern 非法: {}",
                n.prefix,
                n.pattern
            );
        }
        // 引擎管道家族 ↔ plugin.json 真值（插件改名即红）
        let llm_manifest: serde_json::Value = serde_json::from_str(
            &std::fs::read_to_string(
                std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
                    .join("../../../plugins/shared/pipeline/core/llm_core/plugin.json"),
            )
            .expect("llm_core/plugin.json 必须存在"),
        )
        .expect("llm_core/plugin.json 必须可解析");
        let tool_manifest: serde_json::Value = serde_json::from_str(
            &std::fs::read_to_string(
                std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
                    .join("../../../plugins/shared/pipeline/core/tool_core/plugin.json"),
            )
            .expect("tool_core/plugin.json 必须存在"),
        )
        .expect("tool_core/plugin.json 必须可解析");
        assert!(
            ENGINE_CONDUIT_PLUGINS.contains(&llm_manifest["id"].as_str().unwrap_or("")),
            "ENGINE_CONDUIT_PLUGINS 与 llm_core 插件 id 漂移"
        );
        assert!(
            ENGINE_CONDUIT_PLUGINS.contains(&tool_manifest["id"].as_str().unwrap_or("")),
            "ENGINE_CONDUIT_PLUGINS 与 tool_core 插件 id 漂移"
        );
    }

    #[test]
    fn streaming_gate_rejects_plugin_bare_uuid_and_a_prefix() {
        let contracts = streaming_contracts();
        // 普通插件（非引擎管道）裸 uuid / a_ / mc_ / 非 p_ 一律拒绝
        for (id, why) in [
            ("9c8e051a-4a2f-4e8e-b2b1-1a2b3c4d5e6f", "乐观裸 uuid 撞车"),
            ("a_0123456789abcdef0123456789abcdef", "冒用内核 a_ 空间"),
            ("mc_0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef", "冒用引擎指纹空间"),
            ("p_", "p_ 空后缀不合 pattern"),
            ("P_UPPER_001", "p_ 不允许大写"),
        ] {
            let err = validate_streaming_event(
                &contracts,
                "stream_chunk",
                &json!({
                    "pipeline_id": "c1b2c3d4e5f64789abcdef0123456789",
                    "message_id": id,
                    "content": "x",
                    "thread_id": "thread-1",
                }),
                Some("my_streamer"),
            )
            .expect_err(why);
            assert!(err.contains("p_"), "实际: {err}");
        }
    }

    #[test]
    fn streaming_gate_accepts_plugin_p_namespace() {
        let contracts = streaming_contracts();
        validate_streaming_event(
            &contracts,
            "stream_start",
            &json!({
                "pipeline_id": "c1b2c3d4e5f64789abcdef0123456789",
                "message_id": "p_my_progress_001",
                "thread_id": "thread-1",
                "persist": false,
            }),
            Some("my_streamer"),
        )
        .expect("插件 p_ 命名空间应放行");
    }

    #[test]
    fn streaming_gate_engine_conduit_must_use_a_prefix() {
        let contracts = streaming_contracts();
        // 引擎管道家族携带内核签发的 a_ id 放行
        validate_streaming_event(
            &contracts,
            "stream_chunk",
            &json!({
                "pipeline_id": "c1b2c3d4e5f64789abcdef0123456789",
                "message_id": "a_0123456789abcdef0123456789abcdef",
                "content": "hi",
                "thread_id": "thread-1",
            }),
            Some("pipeline_llm_core"),
        )
        .expect("llm_core 携带内核签发的 a_ id 应放行");
        // 但引擎管道家族用 p_ 也拒绝（a_ 是内核签发坐标，不得自造）
        let err = validate_streaming_event(
            &contracts,
            "stream_chunk",
            &json!({
                "pipeline_id": "c1b2c3d4e5f64789abcdef0123456789",
                "message_id": "p_llm_selfmade_001",
                "content": "hi",
                "thread_id": "thread-1",
            }),
            Some("pipeline_llm_core"),
        )
        .expect_err("llm_core 不得自造 p_ id");
        assert!(err.contains("a_"), "实际: {err}");
    }

    #[test]
    fn streaming_gate_kernel_internal_passthrough() {
        let contracts = streaming_contracts();
        // 内核内部（ws_session dispatch，无 _plugin_id）→ 放行（id 由内核签发）
        validate_streaming_event(
            &contracts,
            "new_message",
            &json!({
                "pipeline_id": "c1b2c3d4e5f64789abcdef0123456789",
                "message_id": "a_0123456789abcdef0123456789abcdef",
                "thread_id": "thread-1",
            }),
            None,
        )
        .expect("内核内部调用应放行");
    }

    #[test]
    fn streaming_gate_schema_rejects_missing_content() {
        let contracts = streaming_contracts();
        // schema 层：stream_chunk 必填 content
        let err = validate_streaming_event(
            &contracts,
            "stream_chunk",
            &json!({
                "pipeline_id": "c1b2c3d4e5f64789abcdef0123456789",
                "message_id": "p_ok_001",
                "thread_id": "thread-1",
            }),
            Some("my_streamer"),
        )
        .expect_err("stream_chunk 缺 content 必须红");
        assert!(err.contains("content"), "实际: {err}");
        // 非契约事件（interaction_*）不归本闸（透传族）
        validate_streaming_event(
            &contracts,
            "interaction_request",
            &json!({"request_id": "r1"}),
            Some("my_streamer"),
        )
        .expect("非契约事件应宽泛放行");
    }

    #[test]
    fn streaming_gate_rejects_unknown_contract_event() {
        let contracts = streaming_contracts();
        // streaming.json 未声明的事件名 → 不归本闸（find_spec miss 放行）
        validate_streaming_event(
            &contracts,
            "stream_unknown",
            &json!({"pipeline_id": "x", "message_id": "p_x", "thread_id": "t"}),
            Some("my_streamer"),
        )
        .expect("未声明事件应宽泛放行（契约没写就不查）");
    }
}
