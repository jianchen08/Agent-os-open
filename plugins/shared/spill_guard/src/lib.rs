//! # spill_guard 原生插件（cdylib，大输出兜底）
//!
//! 对工具的大输出统一处理：**原文存文件 + UTF-8 安全提取 + 存档路径引导**。
//! 取回面 = 通用文件工具直读（file_read 分段 / enhanced_search 检索，
//! ADR 2026-11-18-spill-file-face-direct-read；专用取回工具已退役）；
//! on_pipeline_end 钩子清理本管道存档目录，全局 max_files 封顶兜底崩溃残留。
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
use std::path::{Path, PathBuf};

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

    /// 生命周期钩子处理（best-effort）：on_pipeline_end 清理该管道的 spill
    /// 存档目录，其余钩子 no-op。恒返回 "{}"（钩子无状态产出，内核忽略）。
    fn handle_hook(&self, ectx: &ExecContext, hook: &Value) -> Result<&str, &str> {
        let name = hook.get("name").and_then(|v| v.as_str()).unwrap_or("");
        let pid = hook
            .get("tags")
            .and_then(|t| t.get("pipeline_id"))
            .and_then(|v| v.as_str())
            .unwrap_or("");
        if name == "on_pipeline_end" && !pid.trim().is_empty() {
            let config = ectx.ctx.config_value();
            // cleanup_on_pipeline_end: false 显式关闭（与原 spill_retrieve 钩子
            // 同开关语义，默认开）。
            let opt_out = config
                .get("spill")
                .and_then(|s| s.get("cleanup_on_pipeline_end"))
                .and_then(|v| v.as_bool())
                == Some(false);
            if !opt_out {
                let store = SpillStore::new(base_from_config(&config), false, 6);
                let _ = store.cleanup(pid);
            }
        }
        let buf = unsafe { &mut *self.out_buf.get() };
        buf.clear();
        buf.push_str("{}");
        Ok(buf.as_str())
    }
}

// SAFETY: out_buf 仅在 execute（内核 blocking 线程串行调用，单实例不重入）
// 的 &self 独占借用期间写入；借出的 &str 由调用方同步拷贝消费，无并发写面。
unsafe impl Send for SpillGuard {}
unsafe impl Sync for SpillGuard {}

impl PipelinePlugin for SpillGuard {
    fn execute(&self, ectx: &ExecContext) -> Result<&str, &str> {
        // 生命周期钩子语义（hook_json 约定字段，ADR 2026-11-18-spill-file-face-
        // direct-read）：on_pipeline_end → 清理该管道的 spill 存档目录（原
        // spill_retrieve 插件的职责，工具移除后随存储所有权迁入本插件）。
        if let Some(hook) = ectx.ctx.hook_value() {
            return self.handle_hook(ectx, &hook);
        }
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

/// spill 配置（config/plugins/pipeline_spill_guard/spill_config.yaml 的 `spill` 命名空间）。
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
    /// 全局存档文件数上限（跨 pipeline；0 = 不限）。超限最老先删（2026-11-18
    /// 用户裁定：存档要封顶，多了覆盖最老的）。
    max_files: usize,
    /// 跳过兜底的工具名（配置豁免面）。
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
        let semantic_max_lines = spill
            .get("semantic_max_lines")
            .and_then(|v| v.as_u64())
            .unwrap_or(10) as usize;
        let semantic_max_line_chars = spill
            .get("semantic_max_line_chars")
            .and_then(|v| v.as_u64())
            .unwrap_or(200) as usize;
        let base_path = base_from_config(config);
        let compression = spill
            .get("compression")
            .and_then(|v| v.as_bool())
            .unwrap_or(false);
        let compression_level = spill
            .get("compression_level")
            .and_then(|v| v.as_u64())
            .unwrap_or(6) as u32;
        let max_files = spill
            .get("max_files")
            .and_then(|v| v.as_u64())
            .unwrap_or(100) as usize;
        let skip_tools: Vec<String> = spill
            .get("skip_tools")
            .and_then(|v| v.as_array())
            .map(|a| {
                a.iter()
                    .filter_map(|v| v.as_str().map(String::from))
                    .collect()
            })
            .unwrap_or_default();
        Some(SpillConfig {
            max_inline_bytes,
            head_fraction,
            tail_fraction,
            semantic_max_lines,
            semantic_max_line_chars,
            base_path,
            compression,
            compression_level,
            max_files,
            skip_tools,
        })
    }
}

/// base 路径解析（与 SpillConfig 同源默认）。钩子清理路径独立使用——enabled=false
/// 或配置缺失时也要能按默认 base 清残留。
fn base_from_config(config: &Value) -> PathBuf {
    let configured = config
        .get("spill")
        .and_then(|s| s.get("base_path"))
        .and_then(|v| v.as_str())
        .unwrap_or("./data/spill");
    resolve_base_path(configured)
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
        std::env::current_dir()
            .unwrap_or_else(|_| PathBuf::from("."))
            .join(p)
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
    let pipeline_id = state
        .get("pipeline_id")
        .and_then(|v| v.as_str())
        .unwrap_or("default");

    // 本 step 的 tool 消息 = messages 末尾往前数 results.len() 条 role=tool 消息
    // （tool_core 每结果恰追加一条，顺序一致；multimodal user 消息在后不影响反向计数）。
    let messages = state
        .get("messages")
        .and_then(|v| v.as_array())
        .cloned()
        .unwrap_or_default();
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

    let store = SpillStore::new(
        cfg.base_path.clone(),
        cfg.compression,
        cfg.compression_level,
    );
    let mut new_results: Vec<Value> = results.clone();
    let mut msg_ops: Vec<Value> = Vec::new();
    let mut spilled_any = false;
    // _full_tool_results（tool_core 全文留档，2026-09-09 用户裁定）与 tool_results
    // 同下标对齐。留档原文以 JSON 存档 {call_id}_full，state 留定位符——
    // 轨迹/快照恒小（此前 208MB 全文随 state 落 core 轨迹行 + checkpoint，
    // 是 DB 膨胀与 resume 巨型分配的根因）；tool_cache_writer 按定位符回读。
    let mut full_results: Vec<Value> = state
        .get("_full_tool_results")
        .and_then(|v| v.as_array())
        .cloned()
        .unwrap_or_default();
    let mut full_spilled = false;

    for (i, result) in results.iter().enumerate() {
        // 只兜底成功结果：失败反馈是纠错信号，吞掉会掩盖真实错误（DSH 同则）。
        if !result
            .get("success")
            .and_then(|v| v.as_bool())
            .unwrap_or(false)
        {
            continue;
        }
        let tool_name = result
            .get("tool_name")
            .and_then(|v| v.as_str())
            .unwrap_or("");
        if cfg.skip_tools.iter().any(|s| s == tool_name) {
            continue;
        }
        // 幂等：已 spill 的结果直接跳过（llm_core 轮次重放等场景）。
        if result
            .get("metadata")
            .and_then(|m| m.get("spill"))
            .is_some()
        {
            continue;
        }
        let Some(data) = result.get("data") else {
            continue;
        };
        if data.is_null() {
            continue;
        }
        // 防取回死循环（ADR 2026-11-18）：file_read 直读 spill 存档原文件的
        // 结果不再兜底——否则「读存档 → 超限再存档 → 引导再读」无限递归。
        // 识别 = 结果 data 的 file/path 字段落在 spill base 下（best-effort
        // 前缀比对，未命中时退化为普通 spill，引导语只建议分段读，循环可断）。
        if tool_name == "file_read" && is_spill_archive_read(data, &cfg.base_path) {
            continue;
        }

        // 序列化（与 tool_core messages 内容同源：serde_yaml）后按字节判断。
        let Ok(text) = serde_yaml::to_string(data) else {
            continue;
        };
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
        let mut retainer = TextRetainer::new(TextStrategy::HeadTail {
            head_bytes,
            tail_bytes,
        });
        retainer.push_str(&text);
        let retained: RetainedText = retainer.finish();
        let sem = semantic::extract(&text, cfg.semantic_max_lines, cfg.semantic_max_line_chars);

        let replacement = build_replacement(
            &retained,
            &sem,
            &spill_ref.absolute_path.to_string_lossy(),
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
        metadata
            .as_object_mut()
            .expect("metadata ensured object")
            .insert(
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

        // _full_tool_results[i] 定位符化（缺失/越界/已定位 → 跳过；存档失败
        // 保全文内联，与主结果同 best-effort 语义）。
        if let Some(full) = full_results.get_mut(i) {
            if full.is_object() && full.get("__spilled__").is_none() {
                if let Ok(full_json) = serde_json::to_string(full) {
                    let full_key = format!("{call_id}_full");
                    if let Ok(full_ref) = store.save(pipeline_id, &full_key, &full_json) {
                        *full = json!({
                            "__spilled__": true,
                            "tool_call_id": call_id,
                            "locator": full_ref.locator,
                            "original_bytes": full_ref.original_bytes,
                            "compressed": full_ref.compressed,
                        });
                        full_spilled = true;
                    }
                }
            }
        }

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

    // 全局文件数封顶（写入发生才需要收敛；跨管道最老先删，兜住崩溃残留）。
    if spilled_any || full_spilled {
        store.enforce_max_files(cfg.max_files);
    }
    if !spilled_any && !full_spilled {
        return HashMap::new();
    }
    let mut updates: HashMap<String, Value> = HashMap::new();
    updates.insert("tool_results".into(), json!(new_results));
    if full_spilled {
        updates.insert("_full_tool_results".into(), json!(full_results));
    }
    if !msg_ops.is_empty() {
        updates.insert("messages".into(), json!({ "_ops": msg_ops }));
    }
    updates
}

/// 组装替换文本：预览（头/省略标记/尾）+ 语义提取块 + 存档路径与直读引导
/// （ADR 2026-11-18：spill_retrieve 工具退役，agent 经 file_read/enhanced_search
/// 直接读存档文件，复用成熟文件工具的分段/检索能力）。
fn build_replacement(
    retained: &RetainedText,
    sem: &semantic::SemanticSummary,
    archive_path: &str,
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
            out.push_str(&format!(
                "\n\n...[中间 {} 字节已省略]...\n\n",
                retained.omitted_bytes
            ));
            out.push_str(parts[1]);
        }
    }
    out.push_str("\n\n── 语义提取 ──\n");
    out.push_str(&semantic::format_block(sem, semantic_max_line_chars));
    out.push_str(&format!(
        "\n\n📌 完整原文已存档（{original_bytes} 字节，已省略 {} 字节）：\n{archive_path}\n\
         查看：file_read 的 offset/limit（或 tail）参数分段读取；检索：enhanced_search 检索该文件。",
        retained.omitted_bytes,
    ));
    out
}

/// file_read 结果是否读的是 spill 存档本体（防死循环豁免面）。
///
/// 路径形态归一（分隔符统一 `/`、去 Windows verbatim `//?/` 前缀、小写比对
/// ——base 来自内核 cwd、file 字段来自 fs_tools resolve，两侧大小写/前缀形态
/// 可能不一致）后做目录前缀比对。best-effort。
fn is_spill_archive_read(data: &Value, base: &Path) -> bool {
    let Some(path) = data
        .get("file")
        .and_then(|v| v.as_str())
        .or_else(|| data.get("path").and_then(|v| v.as_str()))
    else {
        return false;
    };
    let norm = |s: &str| {
        s.replace('\\', "/")
            .trim_start_matches("//?/")
            .to_lowercase()
    };
    let f = norm(path);
    let b = norm(&base.to_string_lossy());
    !b.is_empty() && f.starts_with(&format!("{b}/"))
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

        let new_results = updates
            .get("tool_results")
            .expect("tool_results 更新")
            .as_array()
            .unwrap();
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
        // 直读引导（ADR 2026-11-18）：存档绝对路径 + file_read/enhanced_search
        let archive_path = dir
            .path()
            .join("pipe-test")
            .join("call_big1")
            .to_string_lossy()
            .to_string();
        assert!(
            replacement.contains(&archive_path),
            "必须含存档绝对路径: {archive_path}"
        );
        assert!(replacement.contains("file_read"), "必须含 file_read 引导");
        assert!(
            replacement.contains("enhanced_search"),
            "必须含 enhanced_search 引导"
        );
        assert!(
            !replacement.contains("spill_retrieve"),
            "专用取回工具已退役，不得再引用"
        );

        let spill = nr["metadata"]["spill"].as_object().expect("metadata.spill");
        assert_eq!(spill["tool_call_id"], "call_big1");
        // original_bytes = YAML 序列化后的完整原文长度（必然大于裸 output 字符串）
        assert!(
            spill["original_bytes"].as_u64().unwrap() as usize > big_output(8).len(),
            "original_bytes 应为完整序列化原文长度"
        );

        let store = spill_store::SpillStore::new(dir.path(), false, 6);
        let original = store
            .read("pipe-test", "call_big1")
            .expect("spill 原文可读回");
        // YAML block literal（|-）保留真实换行（缩进为呈现层细节）：
        // 逐行断言原文完整性。
        assert!(
            original.contains("line 000000: some build output here"),
            "存档含首行原文"
        );
        assert!(
            original.contains("line 000127: some build output here"),
            "存档含末行原文（完整存档）"
        );
    }

    #[test]
    fn messages_ops_overwrite_tool_message() {
        let dir = tempfile::tempdir().unwrap();
        let (r, m) = bash_result("call_big2", &big_output(8));
        let updates = run(
            &make_state(vec![r], vec![m.clone()]),
            &test_config(dir.path()),
        );

        let ops = updates
            .get("messages")
            .expect("messages 更新")
            .get("_ops")
            .and_then(|o| o.as_array())
            .expect("_ops 数组");
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
        assert_eq!(
            msg["tool_result"]["metadata"]["spill"]["tool_call_id"],
            "call_big2"
        );
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
        assert!(
            replacement.contains("compilation failed"),
            "语义提取的错误行必须在替换文本中"
        );
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
        assert!(
            !replacement.contains('\u{FFFD}'),
            "替换文本不得有替换字符：{replacement}"
        );
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
            &make_state(
                vec![small_r, big_r, failed],
                vec![small_m, big_m, failed_msg],
            ),
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
        let updates = run(
            &make_state(vec![result], vec![msg]),
            &test_config(dir.path()),
        );
        assert!(updates.is_empty(), "已 spill 标记 → 跳过（幂等）");
    }

    #[test]
    fn full_results_located_when_spilled() {
        let dir = tempfile::tempdir().unwrap();
        let (r, m) = bash_result("call_big3", &big_output(8));
        let full = json!({
            "tool_name": "bash_execute",
            "success": true,
            "error": null,
            "data": {"output": big_output(8)},
            "metadata": null,
            "duration_ms": 12.5,
        });
        let mut state = make_state(vec![r], vec![m]);
        state
            .as_object_mut()
            .unwrap()
            .insert("_full_tool_results".into(), json!([full]));
        let updates = run(&state, &test_config(dir.path()));

        let located = updates["_full_tool_results"][0]
            .as_object()
            .expect("定位符对象");
        assert_eq!(located["__spilled__"], true);
        assert_eq!(located["tool_call_id"], "call_big3");
        let locator = located["locator"].as_str().unwrap();
        assert!(
            locator.ends_with("call_big3_full"),
            "定位符指向 _full 存档: {locator}"
        );

        // 存档可回读且 JSON 结构完整（tool_cache_writer 依赖原样重建 ToolResult）
        let store = spill_store::SpillStore::new(dir.path(), false, 6);
        let text = store
            .read("pipe-test", "call_big3_full")
            .expect("_full 存档可读");
        let back: Value = serde_json::from_str(&text).expect("存档必须是合法 JSON");
        assert_eq!(back["tool_name"], "bash_execute");
        assert!(
            back["data"]["output"]
                .as_str()
                .unwrap()
                .starts_with("line 000000"),
            "全文 data 原样保留"
        );
    }

    #[test]
    fn full_results_absent_key_not_added() {
        let dir = tempfile::tempdir().unwrap();
        let (r, m) = bash_result("call_big4", &big_output(8));
        let updates = run(&make_state(vec![r], vec![m]), &test_config(dir.path()));
        assert!(
            !updates.contains_key("_full_tool_results"),
            "state 无 _full_tool_results 时不得凭空造键"
        );
    }

    #[test]
    fn full_already_located_untouched() {
        let dir = tempfile::tempdir().unwrap();
        let (r, m) = bash_result("call_big5", &big_output(8));
        let full_marker = json!({
            "__spilled__": true,
            "tool_call_id": "call_prev",
            "locator": "pipe-test/call_prev_full",
            "original_bytes": 9999,
            "compressed": false,
        });
        let mut state = make_state(vec![r], vec![m]);
        state
            .as_object_mut()
            .unwrap()
            .insert("_full_tool_results".into(), json!([full_marker.clone()]));
        let updates = run(&state, &test_config(dir.path()));
        assert!(
            !updates.contains_key("_full_tool_results"),
            "已定位符化的元素无需变更 → 不重复 emit 该键（幂等）"
        );
        assert!(
            updates["tool_results"][0]["metadata"]["spill"].is_object(),
            "主结果本身仍照常兜底"
        );
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

    /// file_read 结果构造（data.file = 读到的绝对路径；对齐 fs_tools 返回面）。
    fn file_read_result(call_id: &str, file: &str, content: &str) -> (Value, Value) {
        let data = json!({ "file": file, "content": content, "lines": 99, "size": content.len() });
        let result = json!({
            "tool_name": "file_read",
            "success": true,
            "error": null,
            "data": data,
            "metadata": null,
            "duration_ms": 3.0,
        });
        let msg = json!({
            "role": "tool",
            "tool_call_id": call_id,
            "content": content,
            "tool_result": {
                "call_id": call_id,
                "tool_name": "file_read",
                "success": true,
                "error": null,
                "data": data,
                "metadata": null,
                "duration_ms": 3.0,
            },
            "seq": 9,
        });
        (result, msg)
    }

    #[test]
    fn file_read_of_archive_not_respilled() {
        // 防死循环（ADR 2026-11-18）：file_read 直读存档原文件 → 不再兜底。
        let dir = tempfile::tempdir().unwrap();
        let archive = dir.path().join("pipe-test").join("call_arch");
        let (r, m) = file_read_result("call_r", &archive.to_string_lossy(), &big_output(30));
        let updates = run(&make_state(vec![r], vec![m]), &test_config(dir.path()));
        assert!(updates.is_empty(), "读存档本体的结果豁免（防递归 spill）");
    }

    #[test]
    fn file_read_of_other_big_file_still_spilled() {
        // 对照组（防拟合）：file_read 读 base 外的大文件 → 照常兜底。
        let dir = tempfile::tempdir().unwrap();
        let outside = tempfile::tempdir().unwrap(); // 与 base 无关的目录树
        let elsewhere = outside.path().join("big.log");
        let (r, m) = file_read_result("call_r2", &elsewhere.to_string_lossy(), &big_output(30));
        let updates = run(&make_state(vec![r], vec![m]), &test_config(dir.path()));
        assert!(
            updates.contains_key("tool_results"),
            "base 外大文件读取照常兜底"
        );
    }

    #[test]
    fn archive_path_normalization_tolerates_windows_forms() {
        // 归一化有区分度输入：verbatim 前缀 + 反斜杠 + 大小写混排仍识别为存档。
        let base = Path::new("D:/Data/spill");
        let data = json!({ "file": "\\\\?\\D:\\Data\\Spill\\pipe-1\\call_x" });
        assert!(is_spill_archive_read(&data, base));
        let outside = json!({ "file": "\\\\?\\D:\\Data\\spill-other\\call_x" });
        assert!(!is_spill_archive_read(&outside, base), "兄弟目录不得误判");
        let no_path = json!({ "content": "no file field" });
        assert!(!is_spill_archive_read(&no_path, base));
    }

    #[test]
    fn max_files_cap_evicts_oldest_on_write() {
        // 全局封顶集成：预置 2 个老存档（显式老 mtime），再 spill 1 个新文件，
        // max_files=2 → 最老的预置文件被淘汰。
        let dir = tempfile::tempdir().unwrap();
        let store = SpillStore::new(dir.path(), false, 6);
        store.save("old-pipe", "old1", "ancient").unwrap();
        store.save("old-pipe", "old2", "older").unwrap();
        let old_t = std::time::SystemTime::UNIX_EPOCH;
        for key in ["old1", "old2"] {
            let f = std::fs::OpenOptions::new()
                .write(true)
                .open(dir.path().join("old-pipe").join(key))
                .unwrap();
            f.set_modified(old_t).unwrap();
        }
        let mut cfg = test_config(dir.path());
        cfg["spill"]["max_files"] = json!(2);
        let (r, m) = bash_result("call_new", &big_output(8));
        run(&make_state(vec![r], vec![m]), &cfg);
        assert!(
            !dir.path().join("old-pipe").join("old1").exists(),
            "最老文件被淘汰（目录序确定性）"
        );
        assert!(
            dir.path().join("old-pipe").join("old2").exists(),
            "次新保留"
        );
        assert!(
            dir.path().join("pipe-test").join("call_new").exists(),
            "新存档在"
        );
    }

    #[test]
    fn hook_on_pipeline_end_cleans_archive_dir() {
        // on_pipeline_end 钩子（经 execute 的 hook_json 约定字段）：清理该管道
        // 存档目录，返回 "{}"；其它管道不受影响。
        let dir = tempfile::tempdir().unwrap();
        let store = SpillStore::new(dir.path(), false, 6);
        store.save("pipe-a", "k1", "x").unwrap();
        store.save("pipe-b", "k2", "y").unwrap();
        let guard = SpillGuard::new();
        let ectx_hook = ExecContext {
            ctx: agentos_native_sdk::PluginCtx {
                state_json: "{}".to_string(),
                config_json: serde_json::to_string(&test_config(dir.path())).unwrap(),
                hook_json: Some(
                    r#"{"name":"on_pipeline_end","tags":{"pipeline_id":"pipe-a"}}"#.into(),
                ),
                ..Default::default()
            },
            host: None,
        };
        let out = guard.execute(&ectx_hook).expect("hook execute ok");
        assert_eq!(out, "{}", "钩子无状态产出");
        assert!(!dir.path().join("pipe-a").exists(), "目标管道目录已清理");
        assert!(
            dir.path().join("pipe-b").join("k2").exists(),
            "他人管道不动"
        );
    }

    #[test]
    fn hook_unknown_name_is_noop() {
        let dir = tempfile::tempdir().unwrap();
        let store = SpillStore::new(dir.path(), false, 6);
        store.save("pipe-a", "k1", "x").unwrap();
        let guard = SpillGuard::new();
        let ectx = ExecContext {
            ctx: agentos_native_sdk::PluginCtx {
                state_json: "{}".to_string(),
                config_json: "{}".to_string(),
                hook_json: Some(r#"{"name":"on_load","tags":{}}"#.into()),
                ..Default::default()
            },
            host: None,
        };
        let out = guard.execute(&ectx).expect("hook execute ok");
        assert_eq!(out, "{}");
        assert!(
            dir.path().join("pipe-a").join("k1").exists(),
            "非 end 钩子不清理"
        );
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
        let parsed: HashMap<String, Value> = serde_json::from_str(out).unwrap();
        assert!(parsed.contains_key("tool_results"));
    }
}
