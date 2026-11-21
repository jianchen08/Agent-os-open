//! journal 实录与 op 流（trace mirror / 不变量抑制 / slot ops / messages ops）。

use super::*;
///
/// 非顶层（深层）变更按整体替换（不递归细粒度 diff），对齐 step 级快照语义。
/// messages **不参与 diff**——它走 ops 实录（ops_ledger，插件声明、指纹降级），
/// 全量数组 diff 推断不适用（零兼容）。
/// step 顶层键首触实录：记录写前旧值（首触即定格——后续同键再写不覆盖，
/// diff 只关心 step 边界前后的变化）。调用方在写 state 前调用。
/// 轨迹镜像定位符化的工具结果 staging 键（ADR 2026-09-29-trace-mirror-dedup）。
///
/// 这两键是 tool_core 每个工具调用 step 的 state_updates 全量重写体（数组整体
/// 换新），journal diff 按键忠实镜像即整份原文进 trace 补丁。全文权威副本：
/// `_full_tool_results` → tool_cache_writer 同 run 落工具缓存（缓存真值源）；
/// `tool_results` → 展示截断版随 messages 信封。轨迹只留摘要定位符。
/// DR 无损：回放消费链（merge_patch → merge_recovered_scalars）对这两键
/// （`_` 前缀键被跳过、展示版不参与恢复）零读取。
pub(super) const TRACE_MIRROR_DIGEST_KEYS: &[&str] = &["_full_tool_results", "tool_results"];

/// 构造工具结果数组的轨迹摘要定位符：逐项留 tool_name + 序列化字节数，
/// 不留任何结果内容。数组输入 → `{"_trace_digest":true,count,items:[..]}`；
/// 非数组输入原样透传（防御：键值形状契约外的值不做有损变换）。
pub fn trace_mirror_digest(v: &serde_json::Value) -> serde_json::Value {
    let Some(items) = v.as_array() else {
        return v.clone();
    };
    let item_digests: Vec<serde_json::Value> = items
        .iter()
        .map(|it| {
            serde_json::json!({
                "tool_name": it.get("tool_name").and_then(|t| t.as_str()).unwrap_or(""),
                "chars": serde_json::to_string(it).map(|s| s.len()).unwrap_or(0),
            })
        })
        .collect();
    serde_json::json!({
        "_trace_digest": true,
        "count": items.len(),
        "items": item_digests,
    })
}

/// 管道不变上下文键清单：管道生命周期内内容不变的键。管道每轮 run 重建
/// state，初始化重写（agent_config_load/tool_schema/tool_core 等落键）对
/// step journal 恒为"新增键"→ 全值入 trace；实测 99.2% 实例为同管道字节级
/// 重复（装机库 1125 份 system_message 集中在 96 管道）。`context.` 前缀族
/// （context.agent_name 等）按前缀匹配，其余精确匹配。
const INVARIANT_SUPPRESS_KEYS: &[&str] = &[
    "system_message",
    "tool_output_contracts",
    "tool_schemas",
    "tool_ids",
];

/// 参与同值跳过的最小序列化字节数：小值照常更新（比对收益不值），基线表
/// 也不被海量小键污染。
pub(super) const INVARIANT_SUPPRESS_MIN_BYTES: usize = 4096;

fn is_pipeline_invariant_key(key: &str) -> bool {
    INVARIANT_SUPPRESS_KEYS.contains(&key) || key.starts_with("context.")
}

/// 每管道不变键基线：`(pipeline_id, key) -> 内容哈希`。进程内全局——执行器
/// 每 run 重建，管道跨轮续用要求基线活过执行器实例；重启即清空自愈（重启后
/// 各活跃管道下一轮真写一次全值）。基线只服务于"同值跳过更新"的比对，state
/// 语义零影响；管道首条轨迹恒自含全值，本管道回放完整性不受影响（2026-08-30
/// 管道身份裁定零改动，恢复面无感知）。
pub(super) fn global_invariant_baseline(
) -> &'static parking_lot::Mutex<HashMap<(String, String), u64>> {
    static BASELINE: std::sync::OnceLock<parking_lot::Mutex<HashMap<(String, String), u64>>> =
        std::sync::OnceLock::new();
    BASELINE.get_or_init(|| parking_lot::Mutex::new(HashMap::new()))
}

/// 同值不更新判定（更新层，2026-10-30）：不变清单键且序列化 ≥
/// [`INVARIANT_SUPPRESS_MIN_BYTES`] 且内容哈希与该管道基线一致 → true
/// （调用方**只跳 journal**——没有 journal 就没有 diff，也就没有轨迹，冗余
/// 在更新层消失，而非落库前剔除；set 照常落地，run state 每轮重建，跳写会让
/// 重建 state 永缺该键，见 [`Self::apply_state_key_update`]）。
/// 哈希不一致（真变更）/首写 → 登记基线并返回 false 照常
/// 更新。Null（删除标记）、小值、清单外键、空 pipeline_id（run 极早期，防
/// 跨管道误吞）恒 false。
pub(super) fn invariant_update_suppressed(
    baseline: &mut HashMap<(String, String), u64>,
    pipeline_id: &str,
    key: &str,
    value: &serde_json::Value,
) -> bool {
    if pipeline_id.is_empty() || value.is_null() || !is_pipeline_invariant_key(key) {
        return false;
    }
    let serialized =
        serde_json::to_string(value).expect("serde_json Value serialization is infallible");
    if serialized.len() < INVARIANT_SUPPRESS_MIN_BYTES {
        return false;
    }
    use std::hash::{Hash, Hasher};
    let mut hasher = std::collections::hash_map::DefaultHasher::new();
    serialized.hash(&mut hasher);
    let content_hash = hasher.finish();
    let scope = (pipeline_id.to_string(), key.to_string());
    match baseline.get(&scope) {
        Some(&h) if h == content_hash => true,
        _ => {
            baseline.insert(scope, content_hash);
            false
        }
    }
}

/// 计算 step 执行前后的 state diff：返回 after 中相对 before 变更的顶层 key 及其新值。
/// - 新增的 key：纳入 diff
/// - 值变化的 key：纳入 diff（after 的值）
/// - 值相同的 key：跳过
/// - 键被移除：记 null（RFC 7396 Merge-Patch 语义，回放端 merge_patch 以 null
///   执行删除）——审计账本要求删除可追溯；首触即不存在的（step 内净增净删）
///   净无变化，不记
pub(super) fn state_diff_from_journal(
    journal: &HashMap<String, Option<serde_json::Value>>,
    after: &serde_json::Value,
) -> serde_json::Value {
    let after_obj = match after.as_object() {
        Some(o) => o,
        None => return serde_json::Value::Object(Default::default()),
    };
    let mut diff = serde_json::Map::new();
    for (k, old) in journal {
        if k == "messages" {
            continue; // 轨迹由 ops_ledger 实录提供，diff 不推断
        }
        if k == "_plugin_errors" {
            continue; // 引擎内部键（插件错误收集），不进 trace——回放重建时由 run 开头清空重建
        }
        let Some(v_after) = after_obj.get(k) else {
            // 键被移除：首触已有旧值 → 记 null（merge_patch 语义的删除标记）；
            // 首触即不存在 → step 内净无此键，无可记变化。
            if old.is_some() {
                diff.insert(k.clone(), serde_json::Value::Null);
            }
            continue;
        };
        let changed = match old {
            Some(v_old) => v_old != v_after,
            None => true, // step 内新增 key
        };
        if changed {
            diff.insert(k.clone(), v_after.clone());
        }
    }
    serde_json::Value::Object(diff)
}

/// 把槽位 op（`set`/`insert`，按**稳定 seq** 寻址）应用到**内存稠密数组** `state["messages"]`。
///
/// 与表侧 `SqliteStore::apply_messages_ops_to_table`（稀疏、留 gap）是**同一组 op 的两个落点**：
/// 引擎收到插件 emit 的 op 后，"一次 apply" 同时更新内存 state 与 DB。详见
/// `docs/message_persistence_design.md`。
///
/// 约定：内存数组稠密（删除会紧凑），每个元素自带稳定 `seq` 字段（≠ 数组下标）。
/// - `set(seq, msg)`：msg 为对象 → 找到该 seq 则替换内容（保留 seq=modify）；找不到则按 seq
///   升序插入（append 或填回某 seq）。
/// - `set(seq, null)`：删除该 seq 的元素（数组紧凑，**幸存元素 seq 不变**）。
/// - `insert(at, msg)`：`seq>=at` 的元素 `seq+1`（后段顺延），新元素占 `at`。
pub fn apply_slot_ops_to_array(arr: &mut Vec<serde_json::Value>, ops: &[serde_json::Value]) {
    fn seq_of(m: &serde_json::Value) -> i64 {
        m.get("seq").and_then(|v| v.as_i64()).unwrap_or(i64::MIN)
    }
    fn set_seq(msg: &mut serde_json::Value, seq: i64) {
        if let Some(o) = msg.as_object_mut() {
            o.insert("seq".into(), serde_json::json!(seq));
        }
    }

    for op in ops {
        let kind = op.get("op").and_then(|v| v.as_str()).unwrap_or("");
        match kind {
            "set" => {
                let Some(seq) = op.get("seq").and_then(|v| v.as_i64()) else {
                    continue;
                };
                match op.get("msg") {
                    Some(msg) if msg.is_object() => {
                        let mut new_msg = msg.clone();
                        set_seq(&mut new_msg, seq);
                        if let Some(pos) = arr.iter().position(|m| seq_of(m) == seq) {
                            arr[pos] = new_msg; // modify：同 seq 替换内容
                        } else {
                            // 新 seq：按升序插入（append 或填回特定 seq）
                            let pos = arr
                                .iter()
                                .position(|m| seq_of(m) > seq)
                                .unwrap_or(arr.len());
                            arr.insert(pos, new_msg);
                        }
                    }
                    _ => {
                        // delete：移除该 seq 元素（数组紧凑，幸存元素 seq 不变）
                        if let Some(pos) = arr.iter().position(|m| seq_of(m) == seq) {
                            arr.remove(pos);
                        }
                    }
                }
            }
            "insert" => {
                let Some(at) = op.get("at").and_then(|v| v.as_i64()) else {
                    continue;
                };
                let Some(msg) = op.get("msg") else {
                    continue;
                };
                // 后段顺延：seq>=at 的元素 seq+1
                for m in arr.iter_mut() {
                    let s = seq_of(m);
                    if s >= at {
                        set_seq(m, s + 1);
                    }
                }
                let mut new_msg = msg.clone();
                set_seq(&mut new_msg, at);
                let pos = arr.iter().position(|m| seq_of(m) > at).unwrap_or(arr.len());
                arr.insert(pos, new_msg);
            }
            _ => {}
        }
    }
}

// ── track.messages_chars 记账（W2a 上下文估算账本）─────────────────
//
// `track.messages_chars` = state["messages"] 全体消息的紧凑 JSON UTF-8 字节
// 总和，是管道 YAML 压缩粗门公式的字符账本。引擎在此增量维护（每 op O(1)，
// insert 顺延为 O(n) 但现网零使用），领域公式写在管道 when 里——引擎零领域知识。

/// 单条消息的度量单位：**剔除 seq 后**的紧凑 JSON UTF-8 字节数
/// （`serde_json::to_string(msg).len()`）。
///
/// seq 是引擎分配的槽位元数据而非消息内容：insert op 顺延后段 seq、append
/// 分配新 seq 都不改内容字节——剔除后增量记账与全量重算（[`messages_chars_total`]）
/// 严格一致，两侧共用本函数为唯一定义。
pub(super) fn message_content_bytes(msg: &serde_json::Value) -> i64 {
    let stripped;
    let m = match msg {
        serde_json::Value::Object(o) if o.contains_key("seq") => {
            let mut c = o.clone();
            c.remove("seq");
            stripped = serde_json::Value::Object(c);
            &stripped
        }
        _ => msg,
    };
    serde_json::to_string(m)
        .map(|s| s.len() as i64)
        .unwrap_or(0)
}

/// 全量重算：messages 全体消息的字节总和（冷启动兜底值 + 一致性性质基准）。
pub(super) fn messages_chars_total(state: &serde_json::Value) -> i64 {
    state
        .get("messages")
        .and_then(|v| v.as_array())
        .map(|a| a.iter().map(message_content_bytes).sum())
        .unwrap_or(0)
}

/// 计算一组 messages ops 对字节总和的增量。**必须在 ops 应用前调用**
/// （old 侧读应用前数组）；同批多 op 按序回放（后序 op 的旧值含前序 op 的
/// 效果），与 `apply_messages_op_update` 对同一批的顺序应用一致。
///
/// 增量规则（单位见 [`message_content_bytes`]，seq 剔除口径下 insert 顺延零成本）：
/// - `set` + msg 对象：seq 处有旧消息 → `new - old`；无旧消息（含引擎分配
///   seq 的 append）→ `+new`
/// - `set` + msg=null/缺省（delete）：`-old`（无旧消息为 no-op）
/// - `insert`：`+new`
pub(super) fn messages_ops_chars_delta(
    state: &serde_json::Value,
    ops: &[serde_json::Value],
) -> i64 {
    // 旧值视角 = seq → 内容字节
    let mut seq_bytes: HashMap<i64, i64> = HashMap::new();
    if let Some(arr) = state.get("messages").and_then(|v| v.as_array()) {
        for m in arr {
            if let Some(seq) = m.get("seq").and_then(|s| s.as_i64()) {
                seq_bytes.insert(seq, message_content_bytes(m));
            }
        }
    }
    // 与 apply_messages_op_update 同规则推进 max_seq（无 seq 的 set = append
    // 分配 max+1，显式 seq 推进 max）——同批视角一致。
    let mut max_seq = seq_bytes.keys().copied().max().unwrap_or(-1);
    let mut delta: i64 = 0;
    for op in ops {
        let kind = op.get("op").and_then(|v| v.as_str()).unwrap_or("");
        match kind {
            "set" => match op.get("seq").and_then(|v| v.as_i64()) {
                None => {
                    // append：引擎分配 max+1。resolve 的 max 不看 insert 的 at——
                    // 同批 insert 顺延可把元素挪进该槽位，此时 apply 实为**替换**
                    // （同 seq 替换内容），delta 须扣旧值；纯新槽位 old=0 自然退化为 +new。
                    max_seq += 1;
                    if let Some(msg) = op.get("msg").filter(|m| m.is_object()) {
                        let new = message_content_bytes(msg);
                        let old = seq_bytes.get(&max_seq).copied().unwrap_or(0);
                        delta += new - old;
                        seq_bytes.insert(max_seq, new);
                    }
                    // msg=null 的 append 在 apply 侧为 no-op（分配 seq 无元素可删）——不计
                }
                Some(seq) => {
                    if seq > max_seq {
                        max_seq = seq;
                    }
                    let old = seq_bytes.get(&seq).copied().unwrap_or(0);
                    match op.get("msg") {
                        Some(msg) if msg.is_object() => {
                            let new = message_content_bytes(msg);
                            delta += new - old;
                            seq_bytes.insert(seq, new);
                        }
                        _ => {
                            delta -= old;
                            seq_bytes.remove(&seq);
                        }
                    }
                }
            },
            "insert" => {
                let Some(at) = op.get("at").and_then(|v| v.as_i64()) else {
                    continue;
                };
                // 后段顺延：seq>=at 的键 +1（内容字节不变，仅键移动）
                let shifted: Vec<(i64, i64)> = seq_bytes
                    .iter()
                    .filter(|(s, _)| **s >= at)
                    .map(|(s, b)| (*s, *b))
                    .collect();
                for (s, _) in &shifted {
                    seq_bytes.remove(s);
                }
                for (s, b) in shifted {
                    seq_bytes.insert(s + 1, b);
                }
                if let Some(msg) = op.get("msg") {
                    let new = message_content_bytes(msg);
                    delta += new;
                    seq_bytes.insert(at, new);
                }
                // 注意：不推进 max_seq——apply 的 resolve 只按 seq 字段推进
                // （insert 的 at 不参与），同批视角必须与之一致。
            }
            _ => {}
        }
    }
    delta
}

/// "一次 apply"：把插件 emit 的 messages op **同时**应用到 DB 表与内存 state。
///
/// 这是新模型（op-based）的接线核心：引擎收到插件 `state_updates["messages"]={_ops:[...]}`
/// 后调用本函数——**同一组 op** 既落 `message_slots` 表（`apply_messages_ops_to_table`，
/// 稀疏、留 gap），又更新 `state["messages"]`（`apply_slot_ops_to_array`，
/// 稠密、元素带 seq）。无 mirror、无 diff。详见 `docs/message_persistence_design.md`。
///
/// **写序 = 先落 DB 后动内存**（2026-11-18，B3 user 输入同款语义推广到插件侧）：
/// 表写失败时内存 `messages` 数组零副作用、Err 原样上抛——消灭「内存已注入、
/// 实录缺席」的静默分叉窗口。表侧单事务 all-or-nothing，失败重试无部分写入
/// （内存未动 → 重试 resolve 结果确定同批）。
///
/// 返回**指纹降级实录**（ops 即轨迹）：`[{op, seq, message_id}]`，msg 全文替换为
/// `compute_message_id` 指纹（delete 为 null）——调用方（executor）把它落进 step 轨迹，
/// 轨迹因此是"实际运行的实录"而非事后 diff 推断。实录只在表写成功后产出，
/// traces 与 message_slots 天然同源同批。
///
/// - `pipeline_id` 从 `state["pipeline_id"]` 读，为空则只更内存、跳过落表（首轮未注入兜底）。
/// - 内存侧无 `messages` 数组时在表写成功后自动 seed 一个空数组（seed 属内存写面，
///   不前置——表写失败内存零副作用）。
/// - `inject_run_message_id` 置位的 `_assistant_id_*` 粘滞控制键属 per-run 内部键
///   （随 round_start 复位）：表写失败时残留无害——重试重入走粘滞分支注入同一 id，
///   run 终止则 state 整体废弃。
pub async fn apply_messages_op_update(
    state: &mut serde_json::Value,
    store: &dyn agentos_core::traits::StorageBackend,
    tenant_id: &str,
    ops: &[serde_json::Value],
) -> Result<Vec<serde_json::Value>, agentos_core::types::StorageError> {
    if ops.is_empty() {
        return Ok(vec![]);
    }

    if !state.is_object() {
        *state = serde_json::Value::Object(Default::default());
    }

    // 1. resolve（纯计算，只读内存 max seq；不动数组）：解析无 seq 的 set（= append），
    //    引擎按 state 现有 max seq 分配递增 seq——append 类插件（llm_core/tool_core/
    //    user push）无需感知 seq，seq 是引擎分配的稳定槽位；显式带 seq 的 op
    //    （modify/fill-gap/insert）照原样透传，并用其 seq 推进 max。
    let mut max_seq = state
        .get("messages")
        .and_then(|v| v.as_array())
        .map(|a| {
            a.iter()
                .filter_map(|m| m.get("seq").and_then(|s| s.as_i64()))
                .max()
                .unwrap_or(-1)
        })
        .unwrap_or(-1);
    // A1 注入判定基准：resolve 前的旧 max（新槽位 = append 的判据）。
    let entry_max_seq = max_seq;
    let resolved: Vec<serde_json::Value> = ops
        .iter()
        .map(|op| {
            let kind = op.get("op").and_then(|v| v.as_str()).unwrap_or("");
            let explicit = op.get("seq").and_then(|v| v.as_i64());
            match (kind, explicit) {
                ("set", None) => {
                    max_seq += 1;
                    let mut o = op.clone();
                    if let Some(obj) = o.as_object_mut() {
                        obj.insert("seq".into(), serde_json::json!(max_seq));
                    }
                    o
                }
                (_, Some(s)) => {
                    if s > max_seq {
                        max_seq = s;
                    }
                    op.clone()
                }
                _ => op.clone(),
            }
        })
        .collect();

    // 2. 表先写（DB-first）：同一组 op 落 message_slots（pipeline_id 为空则跳过）。
    //    Err 即返——此刻内存 messages 数组零副作用。提取为 owned String，避免对
    //    state 的不可变借用跨越后续可变借用（E0502）。
    let pipeline_id = state
        .get("pipeline_id")
        .and_then(|v| v.as_str())
        .map(str::to_string)
        .unwrap_or_default();
    if !pipeline_id.is_empty() {
        // A1：本轮首个 assistant 追加 op 携带内核 message_id（`_message_id` 内部
        // 字段挂 op 上），落表时优先用它作 record_id——流式占位（a_<uuid>）与
        // DB 重载记录 id 对齐，前端去重不再依赖 role::seq 指纹兜底。
        let table_ops = inject_run_message_id(state, &resolved, entry_max_seq);
        store
            .apply_messages_ops_to_table(&pipeline_id, tenant_id, &table_ops)
            .await?;
    }

    // 3. 内存后应用（表写成功 / pipeline_id 为空才到达）：seed 空数组 + 稠密数组 apply
    let need_seed = !matches!(state.get("messages"), Some(v) if v.is_array());
    if need_seed {
        state
            .as_object_mut()
            .expect("ensured object above")
            .insert("messages".into(), serde_json::Value::Array(vec![]));
    }
    if let Some(a) = state.get_mut("messages").and_then(|v| v.as_array_mut()) {
        apply_slot_ops_to_array(a, &resolved);
    }

    // 4. 实录：msg → 指纹降级（compute_message_id 规范化排除 seq，带不带 seq 同指纹）
    Ok(resolved.iter().filter_map(op_ledger_entry).collect())
}

/// A1：把内核 message_id 注入"本轮首个 assistant 追加 op"，并对该槽位本轮内的
/// 后续改写粘滞携带同一 id。
///
/// 注入以 `_message_id` 内部字段挂在 **op** 上而非消息体上：消息是不可变值
/// （blob 内容寻址、LLM 上下文、指纹实录都不应携带它），仅表侧 record_id 消费。
///
/// 认领命中条件（缺一不可，防止误注入历史消息）：
/// - `set` op 且 msg.role == "assistant" 且 msg 未自带 id；
/// - 新槽位追加（op.seq > resolve 前旧 max）——context_window_guard 等对旧槽位的
///   modify op 带显式旧 seq，天然排除；
/// - 每 run 仅一次：`state["_assistant_id_assigned"]` 置位后，多轮迭代的后续
///   assistant 追加（各自独立消息）不再注入，保证 id 与前端流式占位一一对应。
///
/// 粘滞改写：认领槽位 seq 记入 `state["_assistant_id_slot_seq"]`，本轮内对该槽
/// 的显式 seq 改写（post 步骤 [来源:] 标注、收尾改写等）重新注入同一 id——否则
/// 表侧 ON CONFLICT 改写把 record_id 回退成内容指纹，流式占位（a_）与 DB 重载
/// （mc_）键断裂，前端精确对账去重失败成双气泡（2026-10-19 偶发双气泡根因，
/// 装机库 pipeline 9b4da24d92e1 实证：最终回复 seq=11 record_id=mc_71eb12…）。
/// 粘滞槽随 round_start 每轮复位；未认领槽位（真历史/压缩改写）仍走内容派生。
pub(super) fn inject_run_message_id(
    state: &mut serde_json::Value,
    resolved: &[serde_json::Value],
    entry_max_seq: i64,
) -> Vec<serde_json::Value> {
    let Some(mid) = state
        .get("message_id")
        .and_then(|v| v.as_str())
        .filter(|s| !s.is_empty())
    else {
        return resolved.to_vec();
    };
    let assigned = state
        .get("_assistant_id_assigned")
        .and_then(|v| v.as_bool())
        .unwrap_or(false);
    let sticky_seq = state.get("_assistant_id_slot_seq").and_then(|v| v.as_i64());
    // 快路径：本轮 id 已认领且无粘滞槽 → 原样透传
    if assigned && sticky_seq.is_none() {
        return resolved.to_vec();
    }
    let mut out: Vec<serde_json::Value> = resolved.to_vec();
    let mut claimed_slot = sticky_seq;
    // 认领闸门：闩锁已置位或本批已认领一次即关闸（同批多轮 assistant 追加是
    // 各自独立消息，只有首个认领——原实现 break 语义的等价保持）
    let mut claim_open = !assigned;
    let mut newly_claimed = false;
    for op in out.iter_mut() {
        if op.get("op").and_then(|v| v.as_str()) != Some("set") {
            continue;
        }
        let op_seq = op.get("seq").and_then(|v| v.as_i64());
        let is_assistant_obj = op
            .get("msg")
            .map(|m| m.is_object() && m.get("role").and_then(|v| v.as_str()) == Some("assistant"))
            .unwrap_or(false);
        if claim_open {
            // 认领分支：本轮首个 assistant 新槽追加（条件缺一不可）
            let is_new_slot = op_seq.is_some_and(|s| s > entry_max_seq);
            let has_own_id = op
                .get("msg")
                .and_then(|m| m.get("id"))
                .and_then(|v| v.as_str())
                .is_some_and(|s| !s.is_empty());
            if is_new_slot && is_assistant_obj && !has_own_id {
                if let Some(obj) = op.as_object_mut() {
                    obj.insert("_message_id".to_string(), serde_json::json!(mid));
                }
                claimed_slot = op_seq;
                newly_claimed = true;
                claim_open = false;
                continue;
            }
        }
        // 粘滞分支：已认领槽位在本轮内的后续改写重新注入同一 id
        if claimed_slot.is_some() && op_seq == claimed_slot && is_assistant_obj {
            if let Some(obj) = op.as_object_mut() {
                obj.insert("_message_id".to_string(), serde_json::json!(mid));
            }
        }
    }
    if newly_claimed {
        if let Some(obj) = state.as_object_mut() {
            obj.insert(
                "_assistant_id_assigned".to_string(),
                serde_json::json!(true),
            );
            if let Some(ss) = claimed_slot {
                obj.insert("_assistant_id_slot_seq".to_string(), serde_json::json!(ss));
            }
        }
    }
    out
}

/// 把单个已解析 op 降级为轨迹实录条目：`{op, seq, message_id, blob_id}`。
///
/// - `set`：msg 为对象 → message_id = 内容指纹 + blob_id = 全文 blob 定位
///   （回退重建按 blob_id 直查 blobs 取全文，指纹仅审计核对）；msg 为 null/缺省
///   （delete）→ 两者皆 null
/// - `insert`：`{op, at, message_id, blob_id}`
/// - 未知 op：跳过（前向兼容）
///
/// blob_id 与表侧写路径（`write_slot_to_table_locked` 的 `ensure_blob_locked`）同源：
/// 都是 `compute_blob_id(serde_json::to_string(msg))`——同一消息必得同一 blob。
pub(super) fn op_ledger_entry(op: &serde_json::Value) -> Option<serde_json::Value> {
    let kind = op.get("op").and_then(|v| v.as_str()).unwrap_or("");
    let ids = |op: &serde_json::Value| {
        op.get("msg").filter(|m| m.is_object()).map(|m| {
            let blob_src = serde_json::to_string(m).unwrap_or_default();
            (
                agentos_core::ids::compute_message_id(m),
                agentos_core::ids::compute_blob_id(blob_src.as_bytes()),
            )
        })
    };
    match kind {
        "set" => {
            let seq = op.get("seq").and_then(|v| v.as_i64())?;
            let ids = ids(op);
            Some(serde_json::json!({
                "op": "set",
                "seq": seq,
                "message_id": ids.as_ref().map(|(m, _)| m.clone()),
                "blob_id": ids.as_ref().map(|(_, b)| b.clone()),
            }))
        }
        "insert" => {
            let at = op.get("at").and_then(|v| v.as_i64())?;
            let ids = ids(op);
            Some(serde_json::json!({
                "op": "insert",
                "at": at,
                "message_id": ids.as_ref().map(|(m, _)| m.clone()),
                "blob_id": ids.as_ref().map(|(_, b)| b.clone()),
            }))
        }
        _ => None,
    }
}

// ═════════════════════════════════════════════════════════════════
// 单元测试
// ═════════════════════════════════════════════════════════════════

#[cfg(test)]
mod state_diff_journal_tests {
    use super::*;
    use serde_json::json;

    /// journal diff 语义边界三态：变更/新增入选；写回原值与未触碰键、被移除键、
    /// messages/_plugin_errors 一律不进 diff（与旧全量 diff 行为逐条对齐）。
    #[test]
    fn diff_from_journal_semantics() {
        let mut journal = HashMap::new();
        journal.insert("a".to_string(), Some(json!(1))); // 写回原值 → 排除
        journal.insert("b".to_string(), Some(json!(2))); // 变更 → 入选
        journal.insert("c".to_string(), None); // step 内新增 → 入选
        journal.insert("d".to_string(), Some(json!(4))); // 键被移除 → null 入选
        journal.insert("messages".to_string(), Some(json!([]))); // 排除键
        journal.insert("_plugin_errors".to_string(), None); // 排除键
        let after = json!({
            "a": 1,      // 未变
            "b": 3,      // 变更
            "c": 5,      // 新增
            "e": 9,      // 未触碰（不在 journal）→ 排除
        });
        let diff = state_diff_from_journal(&journal, &after);
        assert_eq!(diff, json!({"b": 3, "c": 5, "d": null}));
    }

    /// after 非 Object（防御路径）：diff 为空对象。
    #[test]
    fn diff_from_journal_non_object_after_is_empty() {
        let mut journal = HashMap::new();
        journal.insert("a".to_string(), Some(json!(1)));
        assert_eq!(state_diff_from_journal(&journal, &json!("str")), json!({}));
    }

    /// 键被移除的性质断言：diff 应用到 step 前状态须能重建 after 的键集——
    /// 「首触存在、after 缺失」产出 null 删除标记；「首触即不存在、after 亦无」
    /// 是 step 内净增净删，净无变化，不得产出任何标记。
    #[test]
    fn diff_from_journal_removal_keyset_roundtrip() {
        // 删除可追溯：before 有 x → diff 记 null；消费方按 null 删 x 后键集与 after 一致。
        let mut journal = HashMap::new();
        journal.insert("x".to_string(), Some(json!({"n": 1})));
        journal.insert("keep".to_string(), Some(json!(1)));
        let after = json!({"keep": 2});
        let diff = state_diff_from_journal(&journal, &after);
        assert_eq!(diff, json!({"x": null, "keep": 2}));

        // 净增净删不记：首触时 x 不存在（None），after 亦无 x → diff 不含 x。
        let mut journal = HashMap::new();
        journal.insert("x".to_string(), None);
        let diff = state_diff_from_journal(&journal, &json!({}));
        assert!(diff.as_object().is_some_and(|o| o.is_empty()));
    }
}

#[cfg(test)]
mod invariant_update_suppress_tests {
    use super::*;
    use serde_json::json;

    /// 构造 ≥ 阈值的可序列化值（文本体 8KB）。
    fn big_value(tag: &str) -> serde_json::Value {
        json!({"text": format!("{tag}-{}", "x".repeat(8192))})
    }

    /// 同管道同值：首判不抑制（登记基线、照常更新），再判抑制（同值不更新）。
    #[test]
    fn same_pipeline_same_value_suppressed_on_second_judgement() {
        let mut baseline = HashMap::new();
        assert!(
            !invariant_update_suppressed(&mut baseline, "p1", "system_message", &big_value("a")),
            "管道首写照常更新"
        );
        assert!(
            invariant_update_suppressed(&mut baseline, "p1", "system_message", &big_value("a")),
            "同管道同值重写 → 跳过更新（没有 update 就没有轨迹）"
        );
    }

    /// 值变更（哈希不一致）→ 不抑制且基线更新：同值跳过永不吞真变更。
    #[test]
    fn changed_value_not_suppressed_and_baseline_updates() {
        let mut baseline = HashMap::new();
        invariant_update_suppressed(&mut baseline, "p1", "system_message", &big_value("v1"));
        assert!(invariant_update_suppressed(
            &mut baseline,
            "p1",
            "system_message",
            &big_value("v1")
        ));
        assert!(
            !invariant_update_suppressed(&mut baseline, "p1", "system_message", &big_value("v2")),
            "变更照常更新"
        );
        // 基线已更新为 v2：回写 v1 也视为变更（基线只认最近一次应用值）
        assert!(!invariant_update_suppressed(
            &mut baseline,
            "p1",
            "system_message",
            &big_value("v1")
        ));
    }

    /// 裁定守护（2026-08-30 管道身份）：基线严格按 pipeline_id 隔离——不同
    /// 管道写同值互不影响，任何管道的首写恒更新，跨管道零耦合。
    #[test]
    fn different_pipeline_not_suppressed() {
        let mut baseline = HashMap::new();
        invariant_update_suppressed(&mut baseline, "p1", "system_message", &big_value("shared"));
        assert!(
            !invariant_update_suppressed(
                &mut baseline,
                "p2",
                "system_message",
                &big_value("shared")
            ),
            "他管道写过的同值不得抑制本管道更新"
        );
    }

    /// 边界：小值（<4KB）不参与；清单外键不受影响；context. 前缀族与精确键
    /// （tool_ids）命中；空 pipeline_id（run 极早期）恒不抑制（防跨管道误吞）。
    #[test]
    fn threshold_list_prefix_and_empty_pipeline_boundaries() {
        let mut baseline = HashMap::new();
        let small = json!({"text": "x".repeat(1024)});
        assert!(!invariant_update_suppressed(
            &mut baseline,
            "p1",
            "system_message",
            &small
        ));
        assert!(!invariant_update_suppressed(
            &mut baseline,
            "p1",
            "prompt",
            &big_value("u")
        ));
        assert!(!invariant_update_suppressed(
            &mut baseline,
            "",
            "system_message",
            &big_value("e")
        ));

        assert!(!invariant_update_suppressed(
            &mut baseline,
            "p1",
            "context.agent_name",
            &big_value("灵汐")
        ));
        assert!(invariant_update_suppressed(
            &mut baseline,
            "p1",
            "context.agent_name",
            &big_value("灵汐")
        ));
        assert!(!invariant_update_suppressed(
            &mut baseline,
            "p1",
            "tool_ids",
            &json!({"pad": "a".repeat(4096)})
        ));
        assert!(invariant_update_suppressed(
            &mut baseline,
            "p1",
            "tool_ids",
            &json!({"pad": "a".repeat(4096)})
        ));
    }

    /// Null（删除标记）恒不抑制：删除语义必须照常应用进 state 与轨迹。
    #[test]
    fn null_delete_marker_not_suppressed() {
        let mut baseline = HashMap::new();
        assert!(!invariant_update_suppressed(
            &mut baseline,
            "p1",
            "system_message",
            &serde_json::Value::Null
        ));
    }

    /// 全局基线接线：跨调用持久（同进程模拟跨轮），与纯函数语义一致。
    ///
    /// 不 clear 全局：并发测试互清共享基线致第二次调用被并发 clear 打回首写
    /// （llvm-cov 插桩窗口实测红，c4689c7b9 同族）。管道 scope ("pw", key)
    /// 仅本测试写入——唯一 scope 下无 residue 可防，clear 反成唯一互踩源。
    #[test]
    fn global_baseline_persists_across_calls() {
        assert!(!invariant_update_suppressed(
            &mut global_invariant_baseline().lock(),
            "pw",
            "tool_schemas",
            &big_value("w")
        ));
        assert!(invariant_update_suppressed(
            &mut global_invariant_baseline().lock(),
            "pw",
            "tool_schemas",
            &big_value("w")
        ));
    }
}
