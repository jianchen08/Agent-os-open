//! # spill 原文文件存储
//!
//! 对齐 DSH LocalSpillStore：原文存文件系统，**不进记忆库**（物理隔离，
//! 避免污染语义检索 + 避免 5MB 走 JSON-RPC/chunk）。
//!
//! 布局：`{base_path}/{pipeline_id}/{tool_call_id}` —— 按 pipeline 隔离，
//! 管道结束整目录清理。gzip 压缩可选（读侧按 gzip magic `1f 8b` 自动识别，
//! 读写两侧无需额外协商）。

use std::io::Write as _;
use std::path::{Path, PathBuf};

use flate2::read::GzDecoder;
use flate2::write::GzEncoder;
use flate2::Compression;

/// 一次成功存档的定位信息。
#[derive(Debug, Clone)]
pub struct SpillRef {
    /// 展示用定位符（相对 base 的 `pipeline_id/key` 形式，`/` 分隔便于跨平台显示）。
    pub locator: String,
    /// 绝对路径（本机调试用）。
    #[allow(dead_code)]
    pub absolute_path: PathBuf,
    /// 是否 gzip 压缩存储。
    pub compressed: bool,
    /// 原文字节数（压缩前）。
    pub original_bytes: usize,
}

/// spill 文件存储。
///
/// read/cleanup 当前由测试与 Python 侧 spill_retrieve 对等实现使用（Rust 侧
/// 保留 API 对称：存/读/清同一存储契约），绝对路径字段供定位符审计。
pub struct SpillStore {
    pub(crate) base: PathBuf,
    compression: bool,
    compression_level: u32,
}

impl SpillStore {
    /// base 为目录基准（相对路径由调用方先解析为绝对路径）。
    pub fn new(base: impl Into<PathBuf>, compression: bool, compression_level: u32) -> Self {
        Self {
            base: base.into(),
            compression,
            // flate2 合法区间 0-9，越界收敛到 6。
            compression_level: compression_level.min(9),
        }
    }

    /// 存档原文。key 经 [`sanitize_key`] 消毒后作文件名（防路径穿越）。
    /// 失败返回 Err（调用方 best-effort：保留原结果，不改变工具成败）。
    pub fn save(&self, pipeline_id: &str, key: &str, text: &str) -> Result<SpillRef, String> {
        let dir = self.pipeline_dir(pipeline_id);
        std::fs::create_dir_all(&dir)
            .map_err(|e| format!("spill create_dir {dir:?} 失败: {e}"))?;
        let path = dir.join(sanitize_key(key));
        let bytes = text.as_bytes();

        if self.compression {
            let mut enc = GzEncoder::new(Vec::new(), Compression::new(self.compression_level));
            enc.write_all(bytes)
                .and_then(|_| enc.finish())
                .map_err(|e| format!("spill gzip 压缩失败: {e}"))
                .and_then(|compressed| {
                    std::fs::write(&path, compressed).map_err(|e| format!("spill 写文件 {path:?} 失败: {e}"))
                })?;
        } else {
            std::fs::write(&path, bytes).map_err(|e| format!("spill 写文件 {path:?} 失败: {e}"))?;
        }

        Ok(SpillRef {
            locator: format!("{}/{}", sanitize_key(pipeline_id), sanitize_key(key)),
            absolute_path: path,
            compressed: self.compression,
            original_bytes: bytes.len(),
        })
    }

    /// 读回原文（按 gzip magic 自动解压）。文件不存在 → Err。
    #[allow(dead_code)]
    pub fn read(&self, pipeline_id: &str, key: &str) -> Result<String, String> {
        let path = self.pipeline_dir(pipeline_id).join(sanitize_key(key));
        let raw = std::fs::read(&path).map_err(|e| format!("spill 读文件 {path:?} 失败: {e}"))?;
        if raw.len() >= 2 && raw[0] == 0x1f && raw[1] == 0x8b {
            let mut dec = GzDecoder::new(&raw[..]);
            let mut out = String::new();
            std::io::Read::read_to_string(&mut dec, &mut out)
                .map_err(|e| format!("spill gzip 解压失败: {e}"))?;
            Ok(out)
        } else {
            String::from_utf8(raw).map_err(|e| format!("spill 原文非 UTF-8: {e}"))
        }
    }

    /// 删除该 pipeline 的整个 spill 目录，返回删除的文件数。目录不存在返回 Ok(0)。
    #[allow(dead_code)]
    pub fn cleanup(&self, pipeline_id: &str) -> Result<usize, String> {
        let dir = self.pipeline_dir(pipeline_id);
        if !dir.exists() {
            return Ok(0);
        }
        let count = count_files(&dir);
        std::fs::remove_dir_all(&dir)
            .map_err(|e| format!("spill 清理 {dir:?} 失败: {e}"))?;
        Ok(count)
    }

    /// pipeline 子目录（pipeline_id 同样消毒，防穿越）。
    fn pipeline_dir(&self, pipeline_id: &str) -> PathBuf {
        self.base.join(sanitize_key(pipeline_id))
    }
}

/// key 消毒：仅保留 [A-Za-z0-9._-]，其余替换为 `_`；连续点号（`..`）打散
/// （防 `../` 穿越 / 分隔符注入 / 父目录字面量）。全非法输入退化为 `spill_`
/// 前缀稳定输出（非空、无路径语义）。
pub fn sanitize_key(key: &str) -> String {
    let mut sanitized: String = key
        .chars()
        .map(|c| {
            if c.is_ascii_alphanumeric() || c == '_' || c == '-' || c == '.' {
                c
            } else {
                '_'
            }
        })
        .collect();
    // 连续点号打散为 `_.`：文件名内的 ".." 无路径语义，但作为 LLM 可控 key
    // 的纵深防御，直接消灭该字面量。
    while sanitized.contains("..") {
        sanitized = sanitized.replace("..", "_.");
    }
    // 纯点号/下划线（"..."、"___"）消毒后仍是特殊路径形态，整体退化。
    if sanitized.trim_matches(['.', '_']).is_empty() {
        format!("spill_{}", key.len())
    } else {
        sanitized
    }
}

/// 递归统计目录下文件数（清理回报用）。
#[allow(dead_code)]
fn count_files(dir: &Path) -> usize {
    let mut n = 0;
    if let Ok(entries) = std::fs::read_dir(dir) {
        for entry in entries.flatten() {
            let p = entry.path();
            if p.is_dir() {
                n += count_files(&p);
            } else {
                n += 1;
            }
        }
    }
    n
}

// ═════════════════════════════════════════════════════════════
// 测试（TDD 规格，先于实现编写）
// ═════════════════════════════════════════════════════════════

#[cfg(test)]
mod tests {
    use super::*;

    fn store(compression: bool) -> (SpillStore, tempfile::TempDir) {
        let dir = tempfile::tempdir().expect("tempdir");
        let s = SpillStore::new(dir.path(), compression, 6);
        (s, dir)
    }

    #[test]
    fn save_writes_file_with_content() {
        let (s, _dir) = store(false);
        let r = s.save("pipe-1", "call_abc", "hello spill").expect("save");
        assert!(!r.compressed);
        assert_eq!(r.original_bytes, "hello spill".len());
        assert_eq!(r.locator, "pipe-1/call_abc");
        assert_eq!(r.absolute_path, s.base.join("pipe-1").join("call_abc"));
        assert_eq!(std::fs::read_to_string(&r.absolute_path).expect("read file"), "hello spill");
    }

    #[test]
    fn save_then_read_roundtrip_plain() {
        let (s, _dir) = store(false);
        s.save("pipe-1", "call_abc", "内容原文\nline2").expect("save");
        assert_eq!(s.read("pipe-1", "call_abc").expect("read"), "内容原文\nline2");
    }

    #[test]
    fn save_then_read_roundtrip_gzip() {
        let (s, _dir) = store(true);
        let text = "repeat me ".repeat(1000);
        let r = s.save("pipe-1", "call_big", &text).expect("save");
        assert!(r.compressed);
        let raw = std::fs::read(&r.absolute_path).expect("raw read");
        assert_eq!(&raw[..2], &[0x1f, 0x8b]);
        assert!(raw.len() < text.len() / 5, "gzip 应显著压缩：{} vs {}", raw.len(), text.len());
        assert_eq!(s.read("pipe-1", "call_big").expect("read"), text);
    }

    #[test]
    fn utf8_content_roundtrip() {
        let (s, _dir) = store(true);
        let text = format!("中文日志\n{}", "🙂".repeat(100));
        s.save("p", "k", &text).expect("save");
        assert_eq!(s.read("p", "k").expect("read"), text);
    }

    #[test]
    fn read_missing_file_is_err() {
        let (s, _dir) = store(false);
        assert!(s.read("nope", "nope").is_err());
    }

    #[test]
    fn key_sanitization_blocks_traversal() {
        assert_eq!(sanitize_key("call_abc"), "call_abc");
        assert_eq!(sanitize_key("call-123.x"), "call-123.x");
        let evil = sanitize_key("../../etc/passwd");
        assert!(!evil.contains('/'), "不得含分隔符：{evil}");
        assert!(!evil.contains('\\'), "不得含反斜杠：{evil}");
        assert!(!evil.contains(".."), "不得残留 ..：{evil}");
        assert!(!sanitize_key("///").is_empty());
        let (s, dir) = store(false);
        let evil2 = sanitize_key("a/b\\c..\\..\\d");
        let r = s.save("pipe", &evil2, "x").expect("save");
        assert!(r.absolute_path.starts_with(dir.path()), "必须落在 base 内");
    }

    #[test]
    fn pipeline_id_also_sanitized() {
        let (s, dir) = store(false);
        let r = s.save("../escape", "k", "x").expect("save");
        assert!(r.absolute_path.starts_with(dir.path()), "pipeline_id 消毒后必须落在 base 内");
    }

    #[test]
    fn cleanup_removes_pipeline_dir_only() {
        let (s, _dir) = store(false);
        s.save("pipe-a", "k1", "one").expect("save");
        s.save("pipe-a", "k2", "two").expect("save");
        s.save("pipe-b", "k3", "three").expect("save");
        let removed = s.cleanup("pipe-a").expect("cleanup");
        assert_eq!(removed, 2);
        assert!(!s.base.join("pipe-a").exists());
        assert!(s.base.join("pipe-b").join("k3").exists());
        assert_eq!(s.cleanup("pipe-a").expect("cleanup again"), 0);
    }

    #[test]
    fn save_overwrites_same_key() {
        let (s, _dir) = store(false);
        s.save("p", "k", "v1").expect("save");
        let r = s.save("p", "k", "v2-longer-value").expect("save");
        assert_eq!(r.original_bytes, "v2-longer-value".len());
        assert_eq!(s.read("p", "k").expect("read"), "v2-longer-value");
    }
}
