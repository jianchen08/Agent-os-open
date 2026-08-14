//! # 语义增强提取（bash/log_compressor 的工具无关子集）
//!
//! retention 的头尾截断保"形状"，语义提取保"重点"——错误行/进度行常在
//! 输出中段，纯头尾会丢。模式集从 `plugins/shared/tools/bash/log_compressor.py`
//! 提取，均为工具无关的通用日志语义：
//! - WARNING：warning:/warn/deprecated/obsolete
//! - ERROR：error:/fatal:/failed/exception/traceback + 进程终止类（Killed/
//!   OOM/段错误/总线错误/core dumped/command not found）
//! - PROGRESS：计数（N/M）、百分比、N of M、N packages（取最近一次）
//! - 最新消息：最后一个非空、非引用、长度 >5 的行（有界截断）
//!
//! 全部输出有界：error_lines ≤ max_error_lines 条、每行 ≤ max_line_chars 字符。

use std::collections::BTreeMap;
use std::sync::OnceLock;

use regex::Regex;

/// 语义提取结果（有界：error_lines 条数与行宽受配置约束）。
#[derive(Debug, Clone, Default)]
pub struct SemanticSummary {
    pub warnings: usize,
    pub errors: usize,
    /// 去重合并后的错误行（"xxx (xN)"），至多 max_error_lines 条。
    pub error_lines: Vec<String>,
    /// 最近一次进度（计数/百分比），无则 None。
    pub progress: Option<String>,
    /// 最新非空输出行（有界截断）。
    pub latest_message: String,
}

/// 模式集（编译一次）。
struct Patterns {
    warning: Vec<Regex>,
    error: Vec<Regex>,
    /// 进度：捕获组拼出 "N/M" / "N%" / "N packages"。
    progress: Vec<Regex>,
    /// 时间戳（错误归一化用）。
    timestamp: Regex,
    /// `:数字`（错误归一化：行号等变化部分）。
    line_num: Regex,
}

fn patterns() -> &'static Patterns {
    static P: OnceLock<Patterns> = OnceLock::new();
    P.get_or_init(|| Patterns {
        warning: [r"(?i)warning:", r"(?i)\bwarn\b", r"(?i)deprecated", r"(?i)obsolete"]
            .iter()
            .map(|s| Regex::new(s).expect("warning regex"))
            .collect(),
        error: [
            r"(?i)error:",
            r"(?i)fatal:",
            r"(?i)\bfailed\b",
            r"(?i)\bexception\b",
            r"(?i)\btraceback\b",
            // 进程被信号/系统杀死：exit_code=137 类场景的对应错误行。
            r"(?i)^killed\s*$",
            r"(?i)out of memory",
            r"(?i)\boom\b",
            r"(?i)segmentation fault|segfault",
            r"(?i)bus error",
            r"(?i)core dumped",
            r"(?i)command not found",
        ]
        .iter()
        .map(|s| Regex::new(s).expect("error regex"))
        .collect(),
        progress: vec![
            // 不要求尾部空白：行尾 "done 100/100" 也是合法进度（改进自 log_compressor
            // 的 `(\d+)/(\d+)\s+`——后者漏掉 EOL 无空格的形态）。
            Regex::new(r"(\d+)\s*/\s*(\d+)").expect("progress count"),
            Regex::new(r"(\d+)%").expect("progress percent"),
            Regex::new(r"(\d+)\s+of\s+(\d+)").expect("progress of"),
            Regex::new(r"(\d+)\s+packages?").expect("progress packages"),
        ],
        timestamp: Regex::new(r"\d{4}-\d{2}-\d{2}[T\s]\d{2}:\d{2}:\d{2}").expect("ts regex"),
        line_num: Regex::new(r":\d+").expect("line regex"),
    })
}

/// 从文本提取语义摘要（对齐 LogCompressor 的工具无关子集）。
///
/// - `max_error_lines`：错误行上限。
/// - `max_line_chars`：单行宽度上限。
pub fn extract(text: &str, max_error_lines: usize, max_line_chars: usize) -> SemanticSummary {
    let p = patterns();
    let mut out = SemanticSummary::default();

    // 错误行去重：归一化（去时间戳/行号）计数，展示原文（首见形态）。
    let mut order: Vec<String> = Vec::new();
    let mut counts: BTreeMap<String, usize> = BTreeMap::new();
    let mut display: BTreeMap<String, String> = BTreeMap::new();

    for line in text.lines() {
        if p.warning.iter().any(|re| re.is_match(line)) {
            out.warnings += 1;
        }
        if p.error.iter().any(|re| re.is_match(line)) {
            out.errors += 1;
            let stripped = line.trim();
            let normalized = normalize_error(stripped, p);
            *counts.entry(normalized.clone()).or_insert(0) += 1;
            display.entry(normalized).or_insert_with(|| clip_line(stripped, max_line_chars));
            if !order.contains(&normalize_error(stripped, p)) {
                order.push(normalize_error(stripped, p));
            }
        }
    }

    // 组装去重错误行（保序 + 计数），取最后 max_error_lines 条（对齐 LogCompressor
    // 的 "最近优先"：列表尾部是最新错误）。
    for norm in &order {
        let n = counts.get(norm).copied().unwrap_or(1);
        let shown = display.get(norm).cloned().unwrap_or_default();
        let entry = if n > 1 { format!("{shown} (x{n})") } else { shown };
        out.error_lines.push(entry);
    }
    if out.error_lines.len() > max_error_lines {
        out.error_lines = out.error_lines.split_off(out.error_lines.len() - max_error_lines);
    }

    // 进度：从最近 50 行倒序找（对齐 LogCompressor.extract_progress）。
    let lines: Vec<&str> = text.lines().collect();
    let recent: Vec<&str> = if lines.len() > 50 { lines[lines.len() - 50..].to_vec() } else { lines.clone() };
    for line in recent.iter().rev() {
        if let Some(m) = p.progress.first().and_then(|re| re.captures(line)) {
            out.progress = Some(format!("{}/{}", &m[1], &m[2]));
            break;
        }
        if let Some(m) = p.progress.get(1).and_then(|re| re.captures(line)) {
            out.progress = Some(format!("{}%", &m[1]));
            break;
        }
        if let Some(m) = p.progress.get(2).and_then(|re| re.captures(line)) {
            out.progress = Some(format!("{}/{}", &m[1], &m[2]));
            break;
        }
        if let Some(m) = p.progress.get(3).and_then(|re| re.captures(line)) {
            out.progress = Some(m.get(0).map(|g| g.as_str().to_string()).unwrap_or_default());
            break;
        }
    }

    // 最新消息：最后一个非空、非 > 引用、长度 >5 的行（对齐 get_latest_message）。
    for line in lines.iter().rev() {
        let s = line.trim();
        if !s.is_empty() && !s.starts_with('>') && s.chars().count() > 5 {
            out.latest_message = clip_line(s, max_line_chars);
            break;
        }
    }

    out
}

/// 把语义摘要组装为多行文本块（空统计也产出"警告: 0, 错误: 0"一行，形状稳定）。
pub fn format_block(s: &SemanticSummary, max_line_chars: usize) -> String {
    let mut lines: Vec<String> = Vec::new();
    lines.push(format!("警告: {}, 错误: {}", s.warnings, s.errors));
    if !s.error_lines.is_empty() {
        lines.push("错误行:".to_string());
        for e in &s.error_lines {
            lines.push(format!("  - {}", clip_line(e, max_line_chars + 8)));
        }
    }
    if let Some(p) = &s.progress {
        lines.push(format!("进度: {p}"));
    }
    if !s.latest_message.is_empty() {
        lines.push(format!("最新: {}", clip_line(&s.latest_message, max_line_chars)));
    }
    lines.join("\n")
}

/// 错误归一化（对齐 LogCompressor._normalize_error）：去时间戳、`:数字` → `:*`、
/// 压空白——时间戳/行号变化不阻止合并。
fn normalize_error(error: &str, p: &Patterns) -> String {
    let no_ts = p.timestamp.replace_all(error, "");
    let no_num = p.line_num.replace_all(&no_ts, ":*");
    no_num.split_whitespace().collect::<Vec<_>>().join(" ")
}

/// 行宽裁剪（字符数，UTF-8 安全）：超宽截断 + "..."。
fn clip_line(s: &str, max_chars: usize) -> String {
    if s.chars().count() <= max_chars {
        return s.to_string();
    }
    let clipped: String = s.chars().take(max_chars).collect();
    format!("{clipped}...")
}

// ═════════════════════════════════════════════════════════════
// 测试（TDD 规格，先于实现编写）
// ═════════════════════════════════════════════════════════════

#[cfg(test)]
mod tests {
    use super::*;

    fn extract_default(text: &str) -> SemanticSummary {
        extract(text, 10, 200)
    }

    #[test]
    fn counts_warnings_and_errors_case_insensitive() {
        let text = "ok line\nWARNING: deprecated api\nError: something failed\nwarn: minor\nFATAL: bad";
        let s = extract_default(text);
        assert_eq!(s.warnings, 2, "WARNING:/deprecated + warn:");
        assert_eq!(s.errors, 2, "Error:/failed + FATAL:");
    }

    #[test]
    fn oom_and_signal_lines_are_errors() {
        let text = "building...\nKilled\nout of memory\nSegmentation fault (core dumped)\ncommand not found: foo";
        let s = extract_default(text);
        assert_eq!(s.errors, 4);
        assert!(s.error_lines.iter().any(|l| l.contains("Killed")));
        assert!(s.error_lines.iter().any(|l| l.contains("Segmentation fault")));
    }

    #[test]
    fn error_lines_deduped_with_count() {
        let text = "error: file not found\nerror: file not found\nerror: timeout";
        let s = extract_default(text);
        assert_eq!(s.errors, 3);
        assert!(s.error_lines.contains(&"error: file not found (x2)".to_string()));
        assert!(s.error_lines.contains(&"error: timeout".to_string()));
        assert_eq!(s.error_lines.len(), 2);
    }

    #[test]
    fn error_dedup_normalizes_timestamps_and_line_numbers() {
        // 模式集忠于 log_compressor（"error:" 形态），非 "error at" 等变体。
        let text = "2026-01-01 10:00:00 error: at foo.py:12 boom\n2026-01-01 11:00:00 error: at foo.py:99 boom";
        let s = extract_default(text);
        assert_eq!(s.errors, 2);
        assert_eq!(s.error_lines.len(), 1, "时间戳/行号归一化后应合并");
        assert!(s.error_lines[0].contains("(x2)"));
    }

    #[test]
    fn error_lines_capped() {
        let mut lines = Vec::new();
        for i in 0..30 {
            lines.push(format!("error: unique failure {i}"));
        }
        let s = extract(&lines.join("\n"), 10, 200);
        assert_eq!(s.error_lines.len(), 10);
        assert_eq!(s.errors, 30);
    }

    #[test]
    fn error_line_width_capped() {
        let long_line = format!("error: {}", "x".repeat(500));
        let s = extract_default(&long_line);
        assert_eq!(s.error_lines.len(), 1);
        assert!(s.error_lines[0].chars().count() <= 200 + 3, "超宽行应截断加省略号");
    }

    #[test]
    fn progress_takes_most_recent_match() {
        let text = "progress 10/100 items\nworking...\ndownloading 50%\ndone 100/100";
        let s = extract_default(text);
        assert_eq!(s.progress.as_deref(), Some("100/100"));
    }

    #[test]
    fn progress_percentage_and_packages() {
        let s = extract_default("npm install\nadded 42 packages");
        assert_eq!(s.progress.as_deref(), Some("42 packages"));
        let s = extract_default("compiling 65%");
        assert_eq!(s.progress.as_deref(), Some("65%"));
    }

    #[test]
    fn progress_none_when_absent() {
        let s = extract_default("plain text\nnothing here");
        assert_eq!(s.progress, None);
    }

    #[test]
    fn latest_message_picks_last_meaningful_line() {
        let text = "first line\nsome real output here\n\n";
        let s = extract_default(text);
        assert_eq!(s.latest_message, "some real output here");
    }

    #[test]
    fn latest_message_skips_quote_and_short_lines() {
        let text = "real output line that is long enough\n> quoted prompt\n> another\nab";
        let s = extract_default(text);
        assert_eq!(s.latest_message, "real output line that is long enough");
    }

    #[test]
    fn latest_message_truncated_to_limit() {
        let long = "z".repeat(300);
        let s = extract(&long, 10, 100);
        assert!(s.latest_message.chars().count() <= 100 + 3);
        assert!(s.latest_message.ends_with("..."));
    }

    #[test]
    fn chinese_error_lines_detected() {
        let text = "正常输出\nERROR: 编译失败\n完成";
        let s = extract_default(text);
        assert_eq!(s.errors, 1);
        assert!(s.error_lines[0].contains("编译失败"));
    }

    #[test]
    fn format_block_shape() {
        let s = SemanticSummary {
            warnings: 1,
            errors: 2,
            error_lines: vec!["error: a (x2)".into(), "error: b".into()],
            progress: Some("50%".into()),
            latest_message: "still running".into(),
        };
        let block = format_block(&s, 200);
        assert!(block.contains("警告: 1"));
        assert!(block.contains("错误: 2"));
        assert!(block.contains("error: a (x2)"));
        assert!(block.contains("进度: 50%"));
        assert!(block.contains("最新: still running"));
    }

    #[test]
    fn format_block_minimal_when_clean() {
        let s = SemanticSummary::default();
        let block = format_block(&s, 200);
        assert!(block.contains("警告: 0"));
        assert!(block.contains("错误: 0"));
        assert!(!block.contains("进度:"));
        assert!(!block.contains("最新:"));
    }
}
