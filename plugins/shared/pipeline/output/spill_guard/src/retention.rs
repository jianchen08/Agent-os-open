//! # retention 算法（DSH TextRetainer/ItemRetainer 的 Rust 移植）
//!
//! 设计（对齐 `@deepseek-ai/dsh-output-retention`）：
//! - **流式 + 有界内存**：prefix 蓄积到上限即停；suffix 滑动窗口只留最后
//!   `suffix_cap` 字节（整块滑出即弃）。任意时刻内存 ≤ caps + 一个 chunk。
//! - **UTF-8 边界安全**：`finish` 时 prefix 尾部若是不完整 codepoint 则回退
//!   （trimTrailingPartialUtf8），suffix 头部若是续字节则跳过
//!   （trimLeadingContinuationUtf8）——切割本身不引入 U+FFFD。
//! - **无省略时整体解码**：头尾切片是人为切分，codepoint 可能横跨，必须
//!   拼回一个 buffer 解码；只有真实存在省略间隙时两侧才是真切口。
//! - **职责分离**：retainer 只报告"留了什么/丢了多少字节"，恢复指引文案
//!   由调用方（lib.rs）组装。

/// 文本留存策略（字节预算）。
///
/// Head/Tail 变体当前未被 spill_guard 主流程使用（主流程用 HeadTail），
/// 作为 retention 库的完整策略面保留（对齐 DSH 三策略契约）。
#[derive(Debug, Clone, Copy)]
#[allow(dead_code)]
pub enum TextStrategy {
    /// 留前 max_bytes 字节。
    Head { max_bytes: usize },
    /// 留后 max_bytes 字节（需读到流末尾）。
    Tail { max_bytes: usize },
    /// 留稳定头尾、省略中间（需读到流末尾）。
    HeadTail { head_bytes: usize, tail_bytes: usize },
}

/// 单次 push 的决策反馈。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct PushDecision {
    /// 本 chunk 是否全量保留（无任何字节被丢弃）。
    pub kept: bool,
    /// 累计：预算是否已造成丢弃。
    pub truncated: bool,
}

/// 文本留存终值。
#[derive(Debug, Clone)]
pub struct RetainedText {
    pub text: String,
    pub truncated: bool,
    /// 因预算（含 UTF-8 边界回退）被省略的精确字节数。
    pub omitted_bytes: usize,
}

/// 字节流留存器：prefix + suffix 双蓄积，内存有界。
pub struct TextRetainer {
    prefix_cap: usize,
    suffix_cap: usize,
    prefix_chunks: Vec<Vec<u8>>,
    prefix_held: usize,
    suffix_chunks: Vec<Vec<u8>>,
    suffix_held: usize,
    total: usize,
}

impl TextRetainer {
    pub fn new(strategy: TextStrategy) -> Self {
        let (prefix_cap, suffix_cap) = match strategy {
            TextStrategy::Head { max_bytes } => (max_bytes, 0),
            TextStrategy::Tail { max_bytes } => (0, max_bytes),
            TextStrategy::HeadTail { head_bytes, tail_bytes } => (head_bytes, tail_bytes),
        };
        Self {
            prefix_cap,
            suffix_cap,
            prefix_chunks: Vec::new(),
            prefix_held: 0,
            suffix_chunks: Vec::new(),
            suffix_held: 0,
            total: 0,
        }
    }

    /// 流式喂入一个 chunk（UTF-8 字节切片，允许跨 chunk 切开 codepoint）。
    pub fn push(&mut self, chunk: &[u8]) -> PushDecision {
        let before = self.total;
        self.total += chunk.len();

        // Prefix：只装到上限为止，本 chunk 其余部分不进 prefix。
        let room = self.prefix_cap.saturating_sub(self.prefix_held);
        let take = room.min(chunk.len());
        if take > 0 {
            self.prefix_chunks.push(chunk[..take].to_vec());
            self.prefix_held += take;
        }

        // Suffix：整块追加，然后滑出已完全离开窗口的前置整块（有界内存）。
        if self.suffix_cap > 0 {
            self.suffix_chunks.push(chunk.to_vec());
            self.suffix_held += chunk.len();
            while let Some(head_len) = self.suffix_chunks.first().map(|c| c.len()) {
                if self.suffix_held.saturating_sub(head_len) >= self.suffix_cap {
                    let dropped = self.suffix_chunks.remove(0);
                    self.suffix_held -= dropped.len();
                } else {
                    break;
                }
            }
            // 首块可能仍持超出窗口的前导字节（单 chunk 大于窗口时）：即时裁掉，
            // 蓄积保持 ≤ suffix_cap（finish 只读最后 suffix_len ≤ cap 字节，不丢内容）。
            if let Some(head) = self.suffix_chunks.first_mut() {
                if self.suffix_held > self.suffix_cap {
                    let excess = self.suffix_held - self.suffix_cap;
                    head.drain(..excess);
                    self.suffix_held -= excess;
                }
            }
        }

        // dropped = 两侧都留不住的字节。与 finish 的 omittedAt 同源计算，
        // push 与 finish 永不矛盾。
        let dropped_this_chunk = self.omitted_at(self.total) > self.omitted_at(before);
        PushDecision {
            kept: !dropped_this_chunk,
            truncated: self.omitted_at(self.total) > 0,
        }
    }

    /// 便利方法：str 按 UTF-8 字节喂入。
    pub fn push_str(&mut self, s: &str) -> PushDecision {
        self.push(s.as_bytes())
    }

    /// 终结：拼接留存字节 + UTF-8 边界修正 + 解码，报告精确省略量。
    pub fn finish(self) -> RetainedText {
        let prefix_len = self.total.min(self.prefix_cap);
        let suffix_len = self.total.saturating_sub(prefix_len).min(self.suffix_cap);

        let prefix = concat(&self.prefix_chunks); // 恰为 prefix_len 字节
        let mut suffix_all = concat(&self.suffix_chunks);
        // suffix 蓄积可能多于最终窗口（首块裁剪的余量）：取最后 suffix_len 字节
        let suffix = if suffix_all.len() > suffix_len {
            suffix_all.split_off(suffix_all.len() - suffix_len)
        } else {
            suffix_all
        };

        let budget_omitted = self.omitted_at(self.total);
        if budget_omitted == 0 {
            // 无省略：头尾切片是人为切分（prefix_len + suffix_len == total），
            // codepoint 可能横跨切点——拼回整体解码，否则完整的字被切成两个 U+FFFD。
            let mut whole = prefix;
            whole.extend_from_slice(&suffix);
            return RetainedText {
                text: String::from_utf8_lossy(&whole).into_owned(),
                truncated: false,
                omitted_bytes: 0,
            };
        }

        // 有真实省略间隙：两侧是真切口，各自修 UTF-8 边界后解码。
        let kept_prefix = trim_trailing_partial_utf8(prefix);
        let kept_suffix = trim_leading_continuation_utf8(suffix);
        // 省略量按**实际返回**的字节计（边界回退让出的字节也算省略）。
        let omitted = self
            .total
            .saturating_sub(kept_prefix.len())
            .saturating_sub(kept_suffix.len());
        let mut bytes = kept_prefix;
        bytes.extend_from_slice(&kept_suffix);
        RetainedText {
            text: String::from_utf8_lossy(&bytes).into_owned(),
            truncated: omitted > 0,
            omitted_bytes: omitted,
        }
    }

    /// 当前留存内存占用（观测/测试用）：prefix + suffix 蓄积字节数。
    #[allow(dead_code)]
    pub fn held_bytes(&self) -> usize {
        self.prefix_held + self.suffix_held
    }

    /// 已见 total 字节时的省略量：total − keptPrefix − keptSuffix。
    fn omitted_at(&self, total: usize) -> usize {
        let prefix_len = total.min(self.prefix_cap);
        let suffix_len = total.saturating_sub(prefix_len).min(self.suffix_cap);
        total.saturating_sub(prefix_len).saturating_sub(suffix_len)
    }
}

/// 去掉尾部不完整 UTF-8 序列：prefix 切点回退到 codepoint 起点。
/// 扫回续字节（10xxxxxx，至多 3 个）找到首字节；若其声明的长度超出剩余
/// 字节则让出该序列。合法完整尾部原样保留。
fn trim_trailing_partial_utf8(mut bytes: Vec<u8>) -> Vec<u8> {
    let mut i = bytes.len() as isize - 1;
    // 从尾向头扫过续字节（至多 3：最长序列 4 字节）
    let start = bytes.len();
    while i >= 0 && (bytes[i as usize] & 0xc0) == 0x80 && start - i as usize <= 3 {
        i -= 1;
    }
    if i < 0 {
        return bytes;
    }
    let lead = bytes[i as usize];
    let expected = if lead < 0x80 {
        1
    } else if lead < 0xe0 {
        2
    } else if lead < 0xf0 {
        3
    } else if lead < 0xf8 {
        4
    } else {
        return bytes; // 非法首字节：不动（源数据畸形交给 lossy 解码）
    };
    if bytes.len() - (i as usize) < expected {
        bytes.truncate(i as usize);
    }
    bytes
}

/// 去掉头部续字节（10xxxxxx）：suffix 切点推进到 codepoint 首字节。
fn trim_leading_continuation_utf8(bytes: Vec<u8>) -> Vec<u8> {
    let mut i = 0;
    while i < bytes.len() && (bytes[i] & 0xc0) == 0x80 {
        i += 1;
    }
    bytes[i..].to_vec()
}

/// 拼接 chunks 为一个连续 buffer。
fn concat(chunks: &[Vec<u8>]) -> Vec<u8> {
    let len: usize = chunks.iter().map(|c| c.len()).sum();
    let mut out = Vec::with_capacity(len);
    for c in chunks {
        out.extend_from_slice(c);
    }
    out
}

/// 逻辑单元（条数）留存终值。
///
/// spill_guard 主流程按字节截断；ItemRetainer 是逻辑单元（条数）留存器，
/// 供后续条目型工具输出（grep/glob/搜索源）复用（对齐 DSH 双留存器分工）。
#[derive(Debug, Clone)]
#[allow(dead_code)]
pub struct RetainedItems<T> {
    pub items: Vec<T>,
    pub truncated: bool,
    pub seen: usize,
    pub kept: usize,
    pub omitted: usize,
}

/// 逻辑单元留存器：按条数留前 max_items（head）。
#[allow(dead_code)]
pub struct ItemRetainer<T> {
    max_items: usize,
    items: Vec<T>,
    seen: usize,
    omitted: usize,
}

/// 逻辑单元留存器实现（条目型工具输出复用，见结构体说明）。
#[allow(dead_code)]
impl<T> ItemRetainer<T> {
    pub fn new(max_items: usize) -> Self {
        Self {
            max_items,
            items: Vec::new(),
            seen: 0,
            omitted: 0,
        }
    }

    pub fn push(&mut self, item: T) -> PushDecision {
        self.seen += 1;
        if self.items.len() < self.max_items {
            // 只在未达上限时到达：此前从未丢弃，truncated 恒 false。
            self.items.push(item);
            return PushDecision { kept: true, truncated: false };
        }
        self.omitted += 1;
        PushDecision { kept: false, truncated: true }
    }

    pub fn finish(self) -> RetainedItems<T> {
        let kept = self.items.len();
        RetainedItems {
            truncated: self.omitted > 0,
            seen: self.seen,
            kept,
            omitted: self.omitted,
            items: self.items,
        }
    }
}

// ═════════════════════════════════════════════════════════════
// 测试（TDD 规格，先于实现编写）
// ═════════════════════════════════════════════════════════════

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn head_short_output_fully_kept() {
        let mut r = TextRetainer::new(TextStrategy::Head { max_bytes: 16 });
        r.push_str("hello");
        let out = r.finish();
        assert_eq!(out.text, "hello");
        assert!(!out.truncated);
        assert_eq!(out.omitted_bytes, 0);
    }

    #[test]
    fn head_tail_short_output_fully_kept() {
        let mut r = TextRetainer::new(TextStrategy::HeadTail { head_bytes: 8, tail_bytes: 8 });
        r.push_str("hello");
        let out = r.finish();
        assert_eq!(out.text, "hello");
        assert!(!out.truncated);
        assert_eq!(out.omitted_bytes, 0);
    }

    #[test]
    fn head_truncates_long_ascii() {
        let mut r = TextRetainer::new(TextStrategy::Head { max_bytes: 5 });
        r.push_str("aaaaaaaaaa");
        let out = r.finish();
        assert_eq!(out.text, "aaaaa");
        assert!(out.truncated);
        assert_eq!(out.omitted_bytes, 5);
    }

    #[test]
    fn tail_keeps_suffix_ascii() {
        let mut r = TextRetainer::new(TextStrategy::Tail { max_bytes: 5 });
        r.push_str("aaaaaaaaaa");
        let out = r.finish();
        assert_eq!(out.text, "aaaaa");
        assert!(out.truncated);
        assert_eq!(out.omitted_bytes, 5);
    }

    #[test]
    fn head_tail_omits_middle() {
        let mut r = TextRetainer::new(TextStrategy::HeadTail { head_bytes: 4, tail_bytes: 4 });
        r.push_str("0123456789");
        let out = r.finish();
        assert_eq!(out.text, "0123".to_string() + "6789");
        assert!(out.truncated);
        assert_eq!(out.omitted_bytes, 2);
    }

    #[test]
    fn zero_budget_omits_everything() {
        let mut r = TextRetainer::new(TextStrategy::Head { max_bytes: 0 });
        let d = r.push_str("abc");
        assert!(!d.kept);
        assert!(d.truncated);
        let out = r.finish();
        assert_eq!(out.text, "");
        assert_eq!(out.omitted_bytes, 3);
    }

    #[test]
    fn push_decision_flags_progressive_drop() {
        let mut r = TextRetainer::new(TextStrategy::Head { max_bytes: 4 });
        assert_eq!(r.push_str("ab"), PushDecision { kept: true, truncated: false });
        assert_eq!(r.push_str("cd"), PushDecision { kept: true, truncated: false });
        assert_eq!(r.push_str("ef"), PushDecision { kept: false, truncated: true });
    }

    #[test]
    fn utf8_head_cut_backs_off_partial_codepoint() {
        let mut r = TextRetainer::new(TextStrategy::Head { max_bytes: 2 });
        r.push_str("中");
        let out = r.finish();
        assert_eq!(out.text, "");
        assert_eq!(out.omitted_bytes, 3);
        assert!(!out.text.contains('\u{FFFD}'), "切割不得引入替换字符");
    }

    #[test]
    fn utf8_tail_cut_skips_leading_continuation() {
        let mut r = TextRetainer::new(TextStrategy::Tail { max_bytes: 2 });
        r.push_str("abc中");
        let out = r.finish();
        assert_eq!(out.text, "");
        assert!(!out.text.contains('\u{FFFD}'));
    }

    #[test]
    fn utf8_head_tail_no_omission_decodes_whole() {
        let mut r = TextRetainer::new(TextStrategy::HeadTail { head_bytes: 3, tail_bytes: 3 });
        r.push_str("中");
        let out = r.finish();
        assert_eq!(out.text, "中");
        assert!(!out.truncated);
    }

    #[test]
    fn utf8_multibyte_head_boundary() {
        let mut r = TextRetainer::new(TextStrategy::Head { max_bytes: 4 });
        r.push_str("中文");
        let out = r.finish();
        assert_eq!(out.text, "中");
        assert!(out.truncated);
        assert_eq!(out.omitted_bytes, 3);
    }

    #[test]
    fn utf8_emoji_survives_head_tail() {
        let text = format!("xx{}{}{}yy", "🙂", "🙂", "🙂");
        let mut r = TextRetainer::new(TextStrategy::HeadTail { head_bytes: 6, tail_bytes: 6 });
        r.push_str(&text);
        let out = r.finish();
        assert!(!out.text.contains('\u{FFFD}'), "emoji 不得被切坏：{:?}", out.text);
        assert!(out.truncated);
    }

    #[test]
    fn streaming_memory_stays_bounded() {
        let chunk = vec![b'a'; 4096];
        let mut r = TextRetainer::new(TextStrategy::HeadTail { head_bytes: 1024, tail_bytes: 1024 });
        let mut total = 0usize;
        for _ in 0..256 {
            r.push(&chunk);
            total += chunk.len();
            assert!(
                r.held_bytes() <= 1024 + 1024 + chunk.len(),
                "held={} 超出上界（caps+chunk）",
                r.held_bytes()
            );
        }
        assert_eq!(total, 1024 * 1024);
        let out = r.finish();
        assert!(out.truncated);
        assert_eq!(out.text.len(), 2048);
        assert_eq!(out.omitted_bytes, total - 2048);
    }

    #[test]
    fn tail_slides_across_chunks() {
        let mut r = TextRetainer::new(TextStrategy::Tail { max_bytes: 5 });
        r.push_str("aaaaa");
        r.push_str("bbbbb");
        let out = r.finish();
        assert_eq!(out.text, "bbbbb");
        assert_eq!(out.omitted_bytes, 5);
    }

    #[test]
    fn single_chunk_larger_than_window_still_bounded() {
        let big = vec![b'x'; 100_000];
        let mut r = TextRetainer::new(TextStrategy::Tail { max_bytes: 1000 });
        r.push(&big);
        assert!(r.held_bytes() <= 1000, "held={}", r.held_bytes());
        let out = r.finish();
        assert_eq!(out.text.len(), 1000);
        assert_eq!(out.omitted_bytes, 99_000);
    }

    #[test]
    fn chunk_boundary_splits_codepoint() {
        let mut r = TextRetainer::new(TextStrategy::Head { max_bytes: 3 });
        r.push(&[0xE4, 0xB8]);
        r.push(&[0xAD]);
        let out = r.finish();
        assert_eq!(out.text, "中");
        assert!(!out.truncated);
    }

    #[test]
    fn item_retainer_keeps_first_n() {
        let mut r = ItemRetainer::new(3);
        for i in 0..5 {
            let d = r.push(i);
            if i < 3 {
                assert!(d.kept && !d.truncated);
            } else {
                assert!(!d.kept && d.truncated);
            }
        }
        let out = r.finish();
        assert_eq!(out.items, vec![0, 1, 2]);
        assert_eq!(out.seen, 5);
        assert_eq!(out.kept, 3);
        assert_eq!(out.omitted, 2);
        assert!(out.truncated);
    }

    #[test]
    fn item_retainer_under_budget_no_truncation() {
        let mut r: ItemRetainer<u32> = ItemRetainer::new(10);
        r.push(1);
        r.push(2);
        let out = r.finish();
        assert_eq!(out.items, vec![1, 2]);
        assert!(!out.truncated);
        assert_eq!(out.omitted, 0);
    }

    #[test]
    fn item_retainer_zero_max() {
        let mut r: ItemRetainer<u8> = ItemRetainer::new(0);
        let d = r.push(7);
        assert!(!d.kept && d.truncated);
        let out = r.finish();
        assert!(out.items.is_empty());
        assert_eq!(out.omitted, 1);
    }
}
