//! # spill_guard 原生插件（cdylib，大输出兜底）
//!
//! 对工具的大输出统一处理：**原文存文件 + UTF-8 安全提取 + 定位符返回**。
//! 与 tool_core 同 step 组合（pipeline 配置保证排在其后）：
//!
//! ```text
//! step: core
//!   plugins: [{{state.core_plugin}}, pipeline_spill_guard]
//!   # tool_core 执行 → 大结果 merge 进内存 state（纯内存，未落轨迹）
//!   # spill_guard 立刻跑 → tool_results/messages 替换成小的（仍同 step）
//!   # step 结束才落轨迹 → 落的是替换后的小结果 ✅
//! ```
//!
//! 职责边界（设计原则）：
//! - 工具只负责"产生内容"和"懂自己的语义提取"，截断兜底是本插件的统一职责
//! - bash 等已有专用提取层（log_compressor）的结果已小 → 阈值内透传（no-op）
//! - best-effort：存档失败不把工具成功变失败（保留原结果）
//! - 配置缺省 = no-op（对齐 DSH spill-policy：配置未注入不拦截）
//!
//! messages 覆盖：tool_core 的 set op 已把全文消息落进内存数组 + message_slots
//! 表（merge 时实时投影），本插件对超阈值消息发**显式 seq 的 set op**——同槽位
//! 覆盖（内存数组替换 + 表行 upsert 到新 blob），step 末尾轨迹/收尾看到的已是
//! 替换后的小结果。

use std::collections::HashMap;
use std::path::PathBuf;

use agentos_native_sdk::{plugin_into_raw, ExecContext, PipelinePlugin};
use serde_json::{json, Value};

mod retention;
mod semantic;
mod spill_store;

use retention::{RetainedText, TextRetainer, TextStrategy};
use spill_store::SpillStore;

/// spill_guard 插件实例。
///
/// 跨分配器契约：execute 结果存自持缓冲（dll 堆），以 `&str` 借给内核读——
/// 不返回 String（内核 drop = 跨堆 free UB）。调用形态=blocking 线程串行，
/// `UnsafeCell` 写读不重叠。
pub struct SpillGuard {
    out_buf: std::cell::UnsafeCell<String>,
}

impl Default for SpillGuard {
    fn default() -> Self {
        Self::new()
    }
}

impl SpillGuard {
    pub fn new() -> Self {
        Self {
            out_buf: std::cell::UnsafeCell::new(String::with_capacity(256 * 1024)),
        }
    }
}

// SAFETY: out_buf 仅在 execute（内核 blocking 线程串行调用，单实例不重入）
// 的 &self 独占借用期间写入；借出的 &str 由调用方同步拷贝消费，无并发写面。
unsafe impl Send for SpillGuard {}
unsafe impl Sync for SpillGuard {}

impl PipelinePlugin for SpillGuard {
    fn execute(&self, ectx: &ExecContext) -> Result<&str, &str> {
        let state = ectx.ctx.state_value();
        let config = ectx.ctx.config_value();
        // run 内部全路径 best-effort：任何存档/解析失败都收敛为空更新（no-op），
        // 不会把 Err 上抛（引擎对插件错误统一 warn+继续，ADR 2026-08-18；此处再加一层自保）。
        let updates = run(&state, &config);
        let buf = unsafe { &mut *self.out_buf.get() };
        // Err 分支同契约（2026-09-02 收口）：错误消息也写自持缓冲借 &str——
        // 旧 `?` 上抛 `format!` 的 String（dll 堆）交内核 drop = 跨堆 free UB，
        // 真机 SIGSEGV（spill_guard 每轮 output 出错即同秒崩）实测。
        match serde_json::to_string(&updates) {
            Ok(json) => {
                *buf = json;
                Ok(buf.as_str())
            }
            Err(e) => {
                *buf = format!("serialize state_updates: {e}");
                Err(buf.as_str())
            }
        }
    }
}

/// 构造函数（extern "C"）：内核 dlopen 后调它拿 trait 对象裸指针。
#[no_mangle]
pub extern "C" fn agentos_plugin_create() -> *mut () {
    plugin_into_raw(SpillGuard::new())
}

/// spill 配置（config/system/spill_config.yaml 的 `spill` 命名空间）。
struct SpillConfig {
    max_inline_bytes: usize,
    /// 预览头部预算占比（0-0.5）。
    head_fraction: f64,
    /// 预览尾部预算占比（0-0.5）。
    tail_fraction: f64,
    semantic_max_lines: usize,
    semantic_max_line_chars: usize,
    base_path: PathBuf,
    compression: bool,
    compression_level: u32,
    /// 跳过兜底的工具名（默认含 spill_retrieve：防 read→spill→read 循环）。
    skip_tools: Vec<String>,
}

impl SpillConfig {
    /// 从注入配置解析。**缺省/禁用/非法 → None（no-op，对齐 DSH）**。
    /// load 时校验：max_inline_bytes 必须为正整数（负/零配置直接禁用并 warn）。
    fn from_config(config: &Value) -> Option<Self> {
        let spill = config.get("spill")?.as_object()?;
        if spill.get("enabled").and_then(|v| v.as_bool()) != Some(true) {
            return None;
        }
        let max_inline_bytes = spill.get("max_inline_bytes").and_then(|v| v.as_u64())? as usize;
        if max_inline_bytes == 0 {
            return None;
        }
        let frac = |key: &str, default: f64| -> f64 {
            spill
                .get(key)
                .and_then(|v| v.as_f64())
                .filter(|f| *f > 0.0 && *f < 1.0)
                .unwrap_or(default)
        };
        let head_fraction = frac("head_fraction", 0.35);
        let tail_fraction = frac("tail_fraction", 0.35);
        let semantic_max_lines =
            spill.get("semantic_max_lines").and_then(|v| v.as_u64()).unwrap_or(10) as usize;
        let semantic_max_line_chars =
            spill.get("semantic_max_line_chars").and_then(|v| v.as_u64()).unwrap_or(200) as usize;
        let base_path = spill
            .get("base_path")
            .and_then(|v| v.as_str())
            .unwrap_or("./data/spill");
        let base_path = resolve_base_path(base_path);
        let compression = spill.get("compression").and_then(|v| v.as_bool()).unwrap_or(true);
        let compression_level = spill.get("compression_level").and_then(|v| v.as_u64()).unwrap_or(6) as u32;
        let mut skip_tools: Vec<String> = spill
            .get("skip_tools")
            .and_then(|v| v.as_array())
            .map(|a| a.iter().filter_map(|v| v.as_str().map(String::from)).collect())
            .unwrap_or_default();
        if !skip_tools.iter().any(|s| s == "spill_retrieve") {
            skip_tools.push("spill_retrieve".into()); // 防 spill_retrieve 自身大输出 → 取回死循环
        }
        Some(SpillConfig {
            max_inline_bytes,
            head_fraction,
            tail_fraction,
            semantic_max_lines,
            semantic_max_line_chars,
            base_path,
            compression,
            compression_level,
            skip_tools,
        })
    }
}

/// 相对 base_path 解析为绝对路径：优先 state 无关的稳定锚点——
/// 1. 环境变量 AGENTOS_SPILL_BASE（显式部署控制）
/// 2. 相对内核进程 cwd（start 脚本从项目根启动，与 ./data/memory 约定一致）
fn resolve_base_path(configured: &str) -> PathBuf {
    if let Ok(explicit) = std::env::var("AGENTOS_SPILL_BASE") {
        if !explicit.trim().is_empty() {
            return PathBuf::from(explicit);
        }
    }
    let p = PathBuf::from(configured);
    if p.is_absolute() {
        p
    } else {
        std::env::current_dir().unwrap_or_else(|_| PathBuf::from(".")).join(p)
    }
}

/// 拦截主流程（纯函数，可测）。
///
/// 读 `state["tool_results"]`（tool_core 刚 merge 进内存的大结果），超阈值的：
/// 原文存档 → TextRetainer headTail 提取 + 语义增强 → 替换 result 与对应
/// tool message（显式 seq 的 set op，覆盖内存 + message_slots 表）。
fn run(state: &Value, config: &Value) -> HashMap<String, Value> {
    let Some(cfg) = SpillConfig::from_config(config) else {
        return HashMap::new();
    };

    let Some(results) = state.get("tool_results").and_then(|v| v.as_array()) else {
        return HashMap::new();
    };
    if results.is_empty() {
        return HashMap::new();
    }
    let pipeline_id = state.get("pipeline_id").and_then(|v| v.as_str()).unwrap_or("default");

    // 本 step 的 tool 消息 = messages 末尾往前数 results.len() 条 role=tool 消息
    // （tool_core 每结果恰追加一条，顺序一致；multimodal user 消息在后不影响反向计数）。
    let messages = state.get("messages").and_then(|v| v.as_array()).cloned().unwrap_or_default();
    let mut batch: Vec<&Value> = Vec::with_capacity(results.len());
    for m in messages.iter().rev() {
        if batch.len() >= results.len() {
            break;
        }
        if m.get("role").and_then(|v| v.as_str()) == Some("tool") {
            batch.push(m);
        }
    }
    batch.reverse(); // 与 results 同序（正序配对）

    let store = SpillStore::new(cfg.base_path.clone(), cfg.compression, cfg.compression_level);
    let mut new_results: Vec<Value> = results.clone();
    let mut msg_ops: Vec<Value> = Vec::new();
    let mut spilled_any = false;

    for (i, result) in results.iter().enumerate() {
        // 只兜底成功结果：失败反馈是纠错信号，吞掉会掩盖真实错误（DSH 同则）。
        if !result.get("success").and_then(|v| v.as_bool()).unwrap_or(false) {
            continue;
        }
        let tool_name = result.get("tool_name").and_then(|v| v.as_str()).unwrap_or("");
        if cfg.skip_tools.iter().any(|s| s == tool_name) {
            continue;
        }
        // 幂等：已 spill 的结果直接跳过（llm_core 轮次重放等场景）。
        if result.get("metadata").and_then(|m| m.get("spill")).is_some() {
            continue;
        }
        let Some(data) = result.get("data") else { continue };
        if data.is_null() {
            continue;
        }

        // 序列化（与 tool_core messages 内容同源：serde_yaml）后按字节判断。
        let Ok(text) = serde_yaml::to_string(data) else { continue };
        if text.len() <= cfg.max_inline_bytes {
            continue;
        }

        // 配对消息（可能缺失：非 tool_core 产出的 tool_results）。
        let msg = batch.get(i).copied();
        let call_id = msg
            .and_then(|m| m.get("tool_call_id").and_then(|v| v.as_str()))
            .map(String::from)
            .unwrap_or_else(|| format!("call_{}", &uuid::Uuid::new_v4().simple().to_string()[..8]));

        // 原文存档（best-effort：失败保留原结果，不改变工具成败）。
        let Ok(spill_ref) = store.save(pipeline_id, &call_id, &text) else {
            continue;
        };

        // 提取：headTail 预算（DSH 同款——为定位符预留开销，替换整体不超过上限）。
        let notice_reserve = 512.min(cfg.max_inline_bytes / 2);
        let budget = cfg.max_inline_bytes.saturating_sub(notice_reserve);
        let head_bytes = (budget as f64 * cfg.head_fraction) as usize;
        let tail_bytes = (budget as f64 * cfg.tail_fraction) as usize;
        let mut retainer = TextRetainer::new(TextStrategy::HeadTail { head_bytes, tail_bytes });
        retainer.push_str(&text);
        let retained: RetainedText = retainer.finish();
        let sem = semantic::extract(&text, cfg.semantic_max_lines, cfg.semantic_max_line_chars);

        let replacement = build_replacement(
            &retained,
            &sem,
            &spill_ref.locator,
            &call_id,
            text.len(),
            cfg.max_inline_bytes,
            cfg.semantic_max_line_chars,
        );

        // 替换 tool_results[i]：data → 提取文本；metadata 合并 spill 标记。
        let nr = &mut new_results[i];
        let mut metadata = nr.get("metadata").cloned().unwrap_or(json!(null));
        if !metadata.is_object() {
            metadata = json!({});
        }
        metadata.as_object_mut().expect("metadata ensured object").insert(
            "spill".into(),
            json!({
                "tool_call_id": call_id,
                "locator": spill_ref.locator,
                "original_bytes": spill_ref.original_bytes,
                "compressed": spill_ref.compressed,
            }),
        );
        nr["data"] = Value::String(replacement.clone());
        nr["metadata"] = metadata.clone();
        spilled_any = true;

        // 覆盖对应 tool 消息（显式 seq set op：内存数组 + message_slots 同槽位替换）。
        if let Some(m) = msg {
            if let Some(seq) = m.get("seq").and_then(|v| v.as_i64()) {
                let mut new_msg = m.clone();
                new_msg["content"] = Value::String(replacement.clone());
                if let Some(env) = new_msg.get_mut("tool_result") {
                    env["data"] = Value::String(replacement.clone());
                    env["metadata"] = metadata.clone();
                }
                msg_ops.push(json!({ "op": "set", "seq": seq, "msg": new_msg }));
            }
        }
    }

    if !spilled_any {
        return HashMap::new();
    }
    let mut updates: HashMap<String, Value> = HashMap::new();
    updates.insert("tool_results".into(), json!(new_results));
    if !msg_ops.is_empty() {
        updates.insert("messages".into(), json!({ "_ops": msg_ops }));
    }
    updates
}

/// 组装替换文本：预览（头/省略标记/尾）+ 语义提取块 + 定位符/取回引导。
fn build_replacement(
    retained: &RetainedText,
    sem: &semantic::SemanticSummary,
    locator: &str,
    call_id: &str,
    original_bytes: usize,
    max_inline_bytes: usize,
    semantic_max_line_chars: usize,
) -> String {
    let mut out = String::with_capacity(max_inline_bytes);
    out.push_str(&format!(
        "[spill_guard] 工具输出 {original_bytes} 字节超过内联上限 {max_inline_bytes}，原文已存档。\n"
    ));
    if !retained.text.is_empty() {
        // 头尾之间插入省略量标记（中段被丢的直观提示）
        let parts: Vec<&str> = split_head_tail(retained, original_bytes);
        out.push_str("\n── 预览（头/尾）──\n");
        out.push_str(parts[0]);
        if parts.len() > 1 {
            out.push_str(&format!("\n\n...[中间 {} 字节已省略]...\n\n", retained.omitted_bytes));
            out.push_str(parts[1]);
        }
    }
    out.push_str("\n\n── 语义提取 ──\n");
    out.push_str(&semantic::format_block(sem, semantic_max_line_chars));
    out.push_str(&format!(
        "\n\n📌 完整原文已存档（{original_bytes} 字节，已省略 {} 字节）：\n\
         调用工具 spill_retrieve(tool_call_id=\"{call_id}\") 取回；存档位置: {locator}。",
        retained.omitted_bytes,
    ));
    out
}

/// 头尾文本拆分：retainer 返回的是拼接文本，这里按已知预算还原头/尾两段
/// （各自已是 UTF-8 安全边界，直接按字符预算近似拆分足够展示用）。
fn split_head_tail(retained: &RetainedText, original_bytes: usize) -> Vec<&str> {
    if !retained.truncated || original_bytes == 0 {
        return vec![&retained.text];
    }
    // 头尾各占一半（与预算占比近似；字符级展示精度足够，字节级真值在 omitted_bytes）
    let total_chars = retained.text.chars().count();
    let head_chars = total_chars / 2;
    let split_at = retained
        .text
        .char_indices()
        .nth(head_chars)
        .map(|(i, _)| i)
        .unwrap_or(retained.text.len());
    vec![&retained.text[..split_at], &retained.text[split_at..]]
}

// ═════════════════════════════════════════════════════════════
// 测试（TDD 规格，先于实现编写）
// ═════════════════════════════════════════════════════════════

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::{json, Value};

    /// 测试用配置：小阈值（2048）+ 临时目录 base。
    fn test_config(base: &std::path::Path) -> Value {
        json!({
            "spill": {
                "enabled": true,
                "max_inline_bytes": 2048,
                "base_path": base.to_string_lossy(),
                "compression": false,
            }
        })
    }

    /// 构造 tool_core 产出的典型 state：tool_results + 配对 messages（带 seq）。
    fn make_state(results: Vec<Value>, msgs: Vec<Value>) -> Value {
        json!({
            "pipeline_id": "pipe-test",
            "core_type": "tool_execute",
            "tool_results": results,
            "messages": msgs,
        })
    }

    /// 单条成功 bash 结果 + 对应 tool message。
    fn bash_result(call_id: &str, output: &str) -> (Value, Value) {
        let data = json!({
            "pid": 123,
            "status": "completed",
            "exit_code": 0,
            "output": output,
        });
        let result = json!({
            "tool_name": "bash_execute",
            "success": true,
            "error": null,
            "data": data,
            "metadata": null,
            "duration_ms": 12.5,
        });
        let content = serde_yaml::to_string(&data).unwrap();
        let msg = json!({
            "role": "tool",
            "tool_call_id": call_id,
            "content": content,
            "tool_result": {
                "call_id": call_id,
                "tool_name": "bash_execute",
                "success": true,
                "error": null,
                "data": data,
                "metadata": null,
                "duration_ms": 12.5,
            },
            "seq": 3,
        });
        (result, msg)
    }

    fn big_output(kb: usize) -> String {
        let mut s = String::with_capacity(kb * 1024);
        for i in 0..(kb * 16) {
            s.push_str(&format!("line {i:06}: some build output here\n"));
        }
        s
    }

    #[test]
    fn noop_when_no_tool_results() {
        let dir = tempfile::tempdir().unwrap();
        let state = json!({"pipeline_id": "p", "messages": []});
        let updates = run(&state, &test_config(dir.path()));
        assert!(updates.is_empty(), "无 tool_results → 空更新");
    }

    #[test]
    fn noop_when_all_results_small() {
        let dir = tempfile::tempdir().unwrap();
        let (r, m) = bash_result("call_small", "echo hi\n");
        let updates = run(&make_state(vec![r], vec![m]), &test_config(dir.path()));
        assert!(updates.is_empty(), "小结果透传，零开销");
    }

    #[test]
    fn noop_when_config_missing() {
        let (r, m) = bash_result("call_big", &big_output(8));
        let updates = run(&make_state(vec![r], vec![m]), &json!({}));
        assert!(updates.is_empty());
    }

    #[test]
    fn noop_when_disabled() {
        let dir = tempfile::tempdir().unwrap();
        let mut cfg = test_config(dir.path());
        cfg["spill"]["enabled"] = json!(false);
        let (r, m) = bash_result("call_big", &big_output(8));
        let updates = run(&make_state(vec![r], vec![m]), &cfg);
        assert!(updates.is_empty());
    }

    #[test]
    fn noop_when_core_plugin_was_llm() {
        let dir = tempfile::tempdir().unwrap();
        let (r, m) = bash_result("call_done", "already small");
        let state = json!({
            "pipeline_id": "p",
            "core_type": "llm_call",
            "tool_results": [r],
            "messages": [m],
        });
        let updates = run(&state, &test_config(dir.path()));
        assert!(updates.is_empty());
    }

    #[test]
    fn big_result_spilled_and_replaced() {
        let dir = tempfile::tempdir().unwrap();
        let (r, m) = bash_result("call_big1", &big_output(8));
        let updates = run(&make_state(vec![r], vec![m]), &test_config(dir.path()));

        let new_results = updates.get("tool_results").expect("tool_results 更新").as_array().unwrap();
        assert_eq!(new_results.len(), 1);
        let nr = &new_results[0];
        assert_eq!(nr["tool_name"], "bash_execute");
        assert_eq!(nr["success"], true, "spill 不得把成功变失败");
        let replacement = nr["data"].as_str().expect("data 替换为提取文本");

        assert!(
            replacement.len() < 2048,
            "替换后 {} 字节应 < 2048",
            replacement.len()
        );
        assert!(replacement.contains("spill_retrieve"), "必须含取回引导");
        assert!(replacement.contains("call_big1"), "必须含 tool_call_id");
        assert!(replacement.contains("pipe-test"), "定位符含 pipeline 隔离");

        let spill = nr["metadata"]["spill"].as_object().expect("metadata.spill");
        assert_eq!(spill["tool_call_id"], "call_big1");
        // original_bytes = YAML 序列化后的完整原文长度（必然大于裸 output 字符串）
        assert!(
            spill["original_bytes"].as_u64().unwrap() as usize > big_output(8).len(),
            "original_bytes 应为完整序列化原文长度"
        );

        let store = spill_store::SpillStore::new(dir.path(), false, 6);
        let original = store.read("pipe-test", "call_big1").expect("spill 原文可读回");
        // YAML block literal（|-）保留真实换行（缩进为呈现层细节）：
        // 逐行断言原文完整性。
        assert!(original.contains("line 000000: some build output here"), "存档含首行原文");
        assert!(original.contains("line 000127: some build output here"), "存档含末行原文（完整存档）");
    }

    #[test]
    fn messages_ops_overwrite_tool_message() {
        let dir = tempfile::tempdir().unwrap();
        let (r, m) = bash_result("call_big2", &big_output(8));
        let updates = run(&make_state(vec![r], vec![m.clone()]), &test_config(dir.path()));

        let ops = updates.get("messages").expect("messages 更新")
            .get("_ops").and_then(|o| o.as_array()).expect("_ops 数组");
        assert_eq!(ops.len(), 1, "一条 tool 消息一个 set op");
        let op = &ops[0];
        assert_eq!(op["op"], "set");
        assert_eq!(op["seq"], 3, "显式 seq 覆盖同槽位（内存 + 表）");
        let msg = &op["msg"];
        assert_eq!(msg["role"], "tool");
        assert_eq!(msg["tool_call_id"], "call_big2");
        let replacement = updates["tool_results"][0]["data"].as_str().unwrap();
        assert_eq!(msg["content"].as_str().unwrap(), replacement);
        assert_eq!(msg["tool_result"]["data"].as_str().unwrap(), replacement);
        assert_eq!(msg["tool_result"]["metadata"]["spill"]["tool_call_id"], "call_big2");
    }

    #[test]
    fn replacement_contains_semantic_lines() {
        let dir = tempfile::tempdir().unwrap();
        let mut output = String::new();
        for i in 0..200 {
            output.push_str(&format!("build step {i:04} ok\n"));
        }
        output.push_str("ERROR: compilation failed at foo.rs:88\n");
        for i in 0..200 {
            output.push_str(&format!("tail noise {i:04}\n"));
        }
        let (r, m) = bash_result("call_sem", &output);
        let updates = run(&make_state(vec![r], vec![m]), &test_config(dir.path()));
        let replacement = updates["tool_results"][0]["data"].as_str().unwrap();
        assert!(replacement.contains("compilation failed"), "语义提取的错误行必须在替换文本中");
        assert!(replacement.contains("错误"));
    }

    #[test]
    fn utf8_chinese_not_corrupted() {
        let dir = tempfile::tempdir().unwrap();
        let mut output = String::new();
        for i in 0..500 {
            output.push_str(&format!("第 {i} 行：中文构建日志输出，内容较长一些\n"));
        }
        let (r, m) = bash_result("call_cn", &output);
        let updates = run(&make_state(vec![r], vec![m]), &test_config(dir.path()));
        let replacement = updates["tool_results"][0]["data"].as_str().unwrap();
        assert!(!replacement.contains('\u{FFFD}'), "替换文本不得有替换字符：{replacement}");
        assert!(replacement.contains("中文构建日志输出"), "头部中文完整保留");
    }

    #[test]
    fn multiple_results_mixed_sizes() {
        let dir = tempfile::tempdir().unwrap();
        let (small_r, small_m) = bash_result("call_s", "tiny");
        let (big_r, big_m) = bash_result("call_b", &big_output(8));
        let failed = json!({
            "tool_name": "bash_execute",
            "success": false,
            "error": "Error: 命令执行失败，退出码: 1",
            "data": null,
            "metadata": null,
            "duration_ms": 3.0,
        });
        let failed_msg = json!({
            "role": "tool",
            "tool_call_id": "call_f",
            "content": "Error: 命令执行失败，退出码: 1",
            "tool_result": {
                "call_id": "call_f",
                "tool_name": "bash_execute",
                "success": false,
                "error": "Error: 命令执行失败，退出码: 1",
                "data": null,
                "metadata": null,
                "duration_ms": 3.0,
            },
            "seq": 5,
        });
        let updates = run(
            &make_state(vec![small_r, big_r, failed], vec![small_m, big_m, failed_msg]),
            &test_config(dir.path()),
        );
        let results = updates["tool_results"].as_array().unwrap();
        assert_eq!(results.len(), 3);
        assert!(results[0]["data"].is_object(), "小结果保持对象形态");
        assert_eq!(results[0]["data"]["output"], "tiny");
        assert!(results[1]["data"].is_string());
        assert_eq!(results[2]["success"], false);
        assert!(results[2]["data"].is_null());
        let ops = updates["messages"]["_ops"].as_array().unwrap();
        assert_eq!(ops.len(), 1);
        assert_eq!(ops[0]["msg"]["tool_call_id"], "call_b");
    }

    #[test]
    fn spill_save_failure_keeps_original() {
        let dir = tempfile::tempdir().unwrap();
        let blocker = dir.path().join("blocker");
        std::fs::write(&blocker, "i am a file").unwrap();
        let (r, m) = bash_result("call_x", &big_output(8));
        let updates = run(&make_state(vec![r], vec![m]), &test_config(&blocker));
        assert!(updates.is_empty(), "存档失败 → 空更新（不改变工具成败）");
    }

    #[test]
    fn already_spilled_result_is_idempotent() {
        let dir = tempfile::tempdir().unwrap();
        let data = json!({"output": "already replaced text ..."});
        let result = json!({
            "tool_name": "bash_execute",
            "success": true,
            "error": null,
            "data": data,
            "metadata": {
                "action": "execute",
                "spill": {"tool_call_id": "call_prev", "locator": "pipe-test/call_prev", "original_bytes": 9999},
            },
            "duration_ms": 1.0,
        });
        let msg = json!({
            "role": "tool",
            "tool_call_id": "call_prev",
            "content": "already replaced text ...",
            "tool_result": {
                "call_id": "call_prev",
                "tool_name": "bash_execute",
                "success": true,
                "error": null,
                "data": data,
                "metadata": result["metadata"].clone(),
                "duration_ms": 1.0,
            },
            "seq": 7,
        });
        let updates = run(&make_state(vec![result], vec![msg]), &test_config(dir.path()));
        assert!(updates.is_empty(), "已 spill 标记 → 跳过（幂等）");
    }

    #[test]
    fn skip_tools_config_exempts_tool() {
        let dir = tempfile::tempdir().unwrap();
        let mut cfg = test_config(dir.path());
        cfg["spill"]["skip_tools"] = json!(["bash_execute"]);
        let (r, m) = bash_result("call_skip", &big_output(8));
        let updates = run(&make_state(vec![r], vec![m]), &cfg);
        assert!(updates.is_empty(), "skip_tools 命中 → 透传");
    }

    #[test]
    fn execute_returns_state_updates_json() {
        let dir = tempfile::tempdir().unwrap();
        let (r, m) = bash_result("call_big9", &big_output(8));
        let state = make_state(vec![r], vec![m]);
        let ectx = ExecContext {
            ctx: agentos_native_sdk::PluginCtx {
                state_json: serde_json::to_string(&state).unwrap(),
                config_json: serde_json::to_string(&test_config(dir.path())).unwrap(),
                ..Default::default()
            },
            host: None,
        };
        let guard = SpillGuard::new();
        let out = guard.execute(&ectx).expect("execute ok");
        let parsed: HashMap<String, Value> = serde_json::from_str(&out).unwrap();
        assert!(parsed.contains_key("tool_results"));
    }
}
