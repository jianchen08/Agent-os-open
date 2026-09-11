//! # 模板插值器
//!
//! 解析配置里的 `{{...}}` 模板表达式，支持从 `state` 取字段以及读取相对路径文件内容。
//!
//! ## 支持的语法
//!
//! - `{{state.xxx}}` —— 从 `state`（`serde_json::Value`）取字段，支持点链，
//!   例如 `{{state.agent_id}}`、`{{state.user.name}}`。
//! - `{{path:相对路径}}` —— 读取文件内容（相对于项目根），例如
//!   `{{path:config/agents/main/persona/agentos_persona.md}}`。
//! - 字段缺失时静默降级为空字符串（不报错）；文件读取失败降级为空串并记
//!   `warn!`（path + error，缺文件也走此路径——配置可缺，静默不可）。
//! - 其它无法识别的表达式当作字面量原样保留。
//!
//! [来源: docs/tasks 0.2 引擎模板插值器]

use std::collections::{HashMap, VecDeque};
use std::path::{Path, PathBuf};
use std::sync::{Mutex, OnceLock};
use std::time::SystemTime;

use serde_json::Value;
use tracing::warn;

/// 解析模板字符串，返回替换后的字符串。
///
/// - `template`: 含 `{{...}}` 的字符串
/// - `state`: 当前状态（`serde_json::Value`）
/// - `project_root`: 项目根，用于 `path:` 解析
///
/// 输出为替换后的字符串。无法识别的表达式原样保留。
pub fn render_template(template: &str, state: &Value, project_root: &Path) -> String {
    // 手写扫描：找出所有 `{{ ... }}` 区段并替换。
    // 不使用 regex 是为了避免给 engine 引入额外依赖。
    let bytes = template.as_bytes();
    let mut out = String::with_capacity(template.len());
    let mut i = 0;

    while i < bytes.len() {
        // 查找下一个 `{{`
        if bytes[i] == b'{' && i + 1 < bytes.len() && bytes[i + 1] == b'{' {
            // 寻找对应的 `}}`
            if let Some(rel_end) = find_close(&bytes[i..]) {
                // rel_end 指向 `}}` 的第一个 `{` 的下标（相对于 i）
                let expr_start = i + 2;
                let expr_end = i + rel_end;
                let expr = &template[expr_start..expr_end];
                let replaced = render_expr(expr.trim(), state, project_root);
                out.push_str(&replaced);
                // 跳过 `{{ ... }}`
                i = i + rel_end + 2;
                continue;
            } else {
                // 没有匹配的 `}}`：剩余部分作为字面量输出
                out.push_str(&template[i..]);
                break;
            }
        }
        // 取下一个 UTF-8 字符并按其字节长度推进（不撕裂多字节字符）
        let next_ch = template[i..].chars().next().expect("non-empty slice");
        out.push(next_ch);
        i += next_ch.len_utf8();
    }

    out
}

/// 在切片中查找 `}}` 的起始下标（即第一个 `}` 的相对位置）。
/// 切片以 `{{` 开头。返回值是 `}` 第一个字符相对位置；找不到返回 `None`。
fn find_close(slice: &[u8]) -> Option<usize> {
    let mut j = 2; // 跳过开头的 `{{`
    while j + 1 < slice.len() {
        if slice[j] == b'}' && slice[j + 1] == b'}' {
            return Some(j);
        }
        j += 1;
    }
    None
}

/// 解析单个表达式（已 trim），返回替换后的字符串。
fn render_expr(expr: &str, state: &Value, project_root: &Path) -> String {
    if let Some(path) = expr.strip_prefix("path:") {
        // 文件读取失败降级为空串（缺文件是配置演进中的常见形态，不阻断执行），
        // 但保留 warn 观测——区别于拼写错误指向不存在文件的静默空值。
        let trimmed = path.trim();
        let full = project_root.join(trimmed);
        read_path_file(&full, trimmed)
    } else if let Some(rest) = expr.strip_prefix("state.") {
        // state 点链取值：不存在返回空串
        state_lookup(state, rest).unwrap_or_default()
    } else {
        // 无法识别 → 当字面量（恢复原始 {{...}} 形式）
        format!("{{{{{}}}}}", expr)
    }
}

/// `{{path:}}` 读取缓存容量（条目数）：persona/骨架等每步热路径文件集合
/// 远小于此，128 条足够覆盖全部活跃模板文件。
const PATH_CACHE_CAPACITY: usize = 128;

/// 缓存条目：内容 + 写入时的 mtime（失效凭据）。
struct CachedContent {
    mtime: SystemTime,
    content: String,
}

/// `{{path:}}` 文件内容缓存：进程内 LRU（容量 [`PATH_CACHE_CAPACITY`]），
/// 按 mtime 失效。
///
/// 取舍（mtime vs TTL 5s）：每步热路径对同一批 persona/骨架文件反复整读，
/// 读整文件远贵于一次 stat——命中只需 stat 校验 mtime；mtime 失效让文件
/// 变更即刻生效（无陈旧窗口），实现同样简单（TTL 需时钟注入才能测试，
/// 且引入最长 5s 读到旧配置的窗口），故选 mtime。粒度取舍：同 mtime 的
/// 内容改写（文件系统时间粒度内）不失效——配置文件低频变更，可接受。
struct PathContentLru {
    entries: HashMap<PathBuf, CachedContent>,
    /// LRU 序：队尾 = 最近使用。容量小（128），O(n) 淘汰可接受。
    lru_order: VecDeque<PathBuf>,
    capacity: usize,
}

impl PathContentLru {
    fn new(capacity: usize) -> Self {
        Self {
            entries: HashMap::new(),
            lru_order: VecDeque::new(),
            capacity,
        }
    }

    /// 取内容：命中（mtime 未变）不触发 reader；否则读盘并缓存。
    /// 读失败不缓存（错误上抛，交调用方按既有契约降级 + warn）。
    fn get_or_read(
        &mut self,
        path: &Path,
        reader: &mut impl FnMut(&Path) -> std::io::Result<String>,
    ) -> std::io::Result<String> {
        let mtime = std::fs::metadata(path).ok().and_then(|m| m.modified().ok());
        if let Some(mtime) = mtime {
            if let Some(entry) = self.entries.get(path) {
                if entry.mtime == mtime {
                    let content = entry.content.clone();
                    self.touch(path);
                    return Ok(content);
                }
            }
        }
        let content = reader(path)?;
        if let Some(mtime) = mtime {
            self.insert(
                path.to_path_buf(),
                CachedContent {
                    mtime,
                    content: content.clone(),
                },
            );
        }
        Ok(content)
    }

    /// 命中晋升到队尾。
    fn touch(&mut self, path: &Path) {
        if let Some(pos) = self.lru_order.iter().position(|p| p == path) {
            self.lru_order.remove(pos);
            self.lru_order.push_back(path.to_path_buf());
        }
    }

    /// 插入新条目；超容量淘汰队首（最久未使用）。
    fn insert(&mut self, path: PathBuf, entry: CachedContent) {
        if self.entries.len() >= self.capacity && !self.entries.contains_key(&path) {
            if let Some(evicted) = self.lru_order.pop_front() {
                self.entries.remove(&evicted);
            }
        }
        self.entries.insert(path.clone(), entry);
        self.touch(&path);
        self.lru_order.push_back(path);
    }

    /// 当前条目数（测试观测面）。
    #[cfg(test)]
    fn len(&self) -> usize {
        self.entries.len()
    }
}

/// 生产读取入口：进程内单例缓存 + 真实文件系统读取。
/// 失败降级为空串并 warn（path + error），与无缓存时行为一致。
fn read_path_file(full: &Path, trimmed: &str) -> String {
    static CACHE: OnceLock<Mutex<PathContentLru>> = OnceLock::new();
    let cache = CACHE.get_or_init(|| Mutex::new(PathContentLru::new(PATH_CACHE_CAPACITY)));
    // 中毒锁就地取值：临界区内无 panic 源，不因既往线程展开永久丢缓存。
    let mut cache = cache
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner);
    match cache.get_or_read(full, &mut |p| std::fs::read_to_string(p)) {
        Ok(content) => content,
        Err(e) => {
            warn!(path = %trimmed, error = %e, "{{path:}} 文件读取失败，降级为空串");
            String::new()
        }
    }
}

/// 从 `state` 按 `a.b.c` 点链逐层取值，返回字符串化结果。
/// 取不到（字段缺失或中间节点非对象）返回 `None`。
fn state_lookup(state: &Value, path: &str) -> Option<String> {
    let mut current = state;
    for key in path.split('.') {
        let key = key.trim();
        if key.is_empty() {
            return None;
        }
        let obj = current.as_object()?;
        current = obj.get(key)?;
    }
    value_to_string(current)
}

/// 把叶子节点 `Value` 转为字符串：String 原样，Number/Bool 取原始字面量。
/// Null/Array/Object 不视为标量叶子，返回 `None`（进而降级为空串）。
fn value_to_string(v: &Value) -> Option<String> {
    match v {
        Value::String(s) => Some(s.clone()),
        Value::Number(n) => Some(n.to_string()),
        Value::Bool(b) => Some(b.to_string()),
        _ => None,
    }
}

/// 递归渲染一个 `serde_json::Value`：对 String 调用 `render_template`，
/// Object/Array 深度遍历渲染每个字符串值，其它类型原样返回。
pub fn render_value(value: &Value, state: &Value, project_root: &Path) -> Value {
    match value {
        Value::String(s) => Value::String(render_template(s, state, project_root)),
        Value::Array(arr) => {
            let mapped: Vec<Value> = arr
                .iter()
                .map(|v| render_value(v, state, project_root))
                .collect();
            Value::Array(mapped)
        }
        Value::Object(obj) => {
            let mapped: serde_json::Map<String, Value> = obj
                .iter()
                .map(|(k, v)| (k.clone(), render_value(v, state, project_root)))
                .collect();
            Value::Object(mapped)
        }
        other => other.clone(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use std::cell::Cell;
    use std::fs;
    use std::io::Write;
    use std::rc::Rc;

    /// 计数桩 reader：统计真实读盘次数（缓存命中不得触发）。
    fn counting_reader(
        content: &'static str,
    ) -> (impl FnMut(&Path) -> std::io::Result<String>, Rc<Cell<u32>>) {
        let reads = Rc::new(Cell::new(0));
        let reads_in_closure = Rc::clone(&reads);
        (
            move |_p: &Path| {
                reads_in_closure.set(reads_in_closure.get() + 1);
                Ok(content.to_string())
            },
            reads,
        )
    }

    #[test]
    fn path_cache_hit_avoids_second_read() {
        // 同一路径两次读取：第二次必须命中缓存（仅一次读盘——计数桩）。
        // 文件须真实存在（缓存以 stat mtime 为失效凭据）；内容由桩返回，
        // 与磁盘内容无关。
        let dir = tempfile::tempdir().expect("create tempdir");
        let path = dir.path().join("whatever.md");
        fs::write(&path, "on disk").expect("write");

        let mut lru = PathContentLru::new(8);
        let (mut reader, reads) = counting_reader("cached body");

        let first = lru.get_or_read(&path, &mut reader).unwrap();
        let second = lru.get_or_read(&path, &mut reader).unwrap();

        assert_eq!(first, "cached body");
        assert_eq!(second, "cached body");
        assert_eq!(reads.get(), 1, "第二次读取必须命中缓存，不得再读盘");
    }

    #[test]
    fn path_cache_invalidates_when_file_mtime_changes() {
        let dir = tempfile::tempdir().expect("create tempdir");
        let file_path = dir.path().join("persona.md");
        fs::write(&file_path, "content-A").expect("write A");

        let mut lru = PathContentLru::new(8);
        let reads = Rc::new(Cell::new(0));
        let reads_in_closure = Rc::clone(&reads);
        let file_for_reader = file_path.clone();
        let mut reader = move |_p: &Path| {
            reads_in_closure.set(reads_in_closure.get() + 1);
            fs::read_to_string(&file_for_reader)
        };

        assert_eq!(
            lru.get_or_read(&file_path, &mut reader).unwrap(),
            "content-A"
        );
        // mtime 粒度兜底：确保写入时间戳前进。
        std::thread::sleep(std::time::Duration::from_millis(20));
        fs::write(&file_path, "content-B").expect("write B");

        assert_eq!(
            lru.get_or_read(&file_path, &mut reader).unwrap(),
            "content-B",
            "文件变更（mtime 变化）后必须重新读盘，不得返回旧内容"
        );
        assert_eq!(reads.get(), 2, "失效后必须真实重读一次");
    }

    #[test]
    fn path_cache_evicts_least_recently_used() {
        // LRU 语义：容量满后淘汰最久未使用者；命中晋升免于淘汰。
        let dir = tempfile::tempdir().expect("create tempdir");
        let mut lru = PathContentLru::new(2);
        let reads = Rc::new(Cell::new(0));
        let reads_in_closure = Rc::clone(&reads);
        let root = dir.path().to_path_buf();
        let mut reader = move |p: &Path| {
            reads_in_closure.set(reads_in_closure.get() + 1);
            fs::read_to_string(root.join(p.file_name().unwrap()))
        };

        let a = dir.path().join("a.txt");
        let b = dir.path().join("b.txt");
        let c = dir.path().join("c.txt");
        fs::write(&a, "A").unwrap();
        fs::write(&b, "B").unwrap();
        fs::write(&c, "C").unwrap();

        lru.get_or_read(&a, &mut reader).unwrap();
        lru.get_or_read(&b, &mut reader).unwrap();
        // 触碰 a → a 晋升，b 成为最久未使用。
        lru.get_or_read(&a, &mut reader).unwrap();
        lru.get_or_read(&c, &mut reader).unwrap(); // 满载 → 淘汰 b
        let reads_after_c = reads.get();

        assert_eq!(lru.len(), 2, "缓存条目数不得超过容量");
        // a 晋升后仍在缓存：读取 a 不触发读盘（LRU 而非 FIFO）。
        lru.get_or_read(&a, &mut reader).unwrap();
        assert_eq!(reads.get(), reads_after_c, "a 晋升后不得被 c 的插入淘汰");
        // b 已被淘汰：读取 b 必须真实重读。
        lru.get_or_read(&b, &mut reader).unwrap();
        assert!(reads.get() > reads_after_c, "b 已被淘汰，必须重新读盘");
    }

    #[test]
    fn path_cache_stays_bounded_beyond_capacity() {
        // 性质断言：灌入远超容量的条目，缓存规模仍钳在容量内。
        let dir = tempfile::tempdir().expect("create tempdir");
        let mut lru = PathContentLru::new(4);
        let mut reader = counting_reader("x").0;
        for i in 0..16 {
            let p = dir.path().join(format!("f{i}.txt"));
            fs::write(&p, "x").unwrap();
            lru.get_or_read(&p, &mut reader).unwrap();
        }
        assert!(lru.len() <= 4, "条目数必须 ≤ 容量，实际: {}", lru.len());
    }

    #[test]
    fn render_path_uses_global_cache_and_reflects_file_change() {
        // 渲染层集成：同一文件两次渲染命中缓存（内容稳定），文件改写后
        // 渲染反映新内容（mtime 失效贯通到 render_template 入口）。
        let dir = tempfile::tempdir().expect("create tempdir");
        let file_path = dir.path().join("persona.md");
        fs::write(&file_path, "v1").expect("write v1");

        let out1 = render_template("{{path:persona.md}}", &json!({}), dir.path());
        let out2 = render_template("{{path:persona.md}}", &json!({}), dir.path());
        assert_eq!(out1, "v1");
        assert_eq!(out2, "v1", "内容未变时渲染结果必须稳定（缓存命中语义）");

        std::thread::sleep(std::time::Duration::from_millis(20));
        fs::write(&file_path, "v2").expect("write v2");
        let out3 = render_template("{{path:persona.md}}", &json!({}), dir.path());
        assert_eq!(out3, "v2", "文件变更后渲染必须反映新内容");
    }

    #[test]
    fn test_render_state_field() {
        let state = json!({ "agent_id": "agent-007" });
        let root = Path::new(".");
        let out = render_template("id={{state.agent_id}}", &state, root);
        assert_eq!(out, "id=agent-007");
    }

    #[test]
    fn test_render_state_dot_chain() {
        let state = json!({
            "user": { "name": "alice", "profile": { "age": 30 } }
        });
        let root = Path::new(".");
        assert_eq!(
            render_template("{{state.user.name}}", &state, root),
            "alice"
        );
        // 数字也应当能字符串化
        assert_eq!(
            render_template("{{state.user.profile.age}}", &state, root),
            "30"
        );
    }

    #[test]
    fn test_render_state_missing() {
        let state = json!({ "agent_id": "x" });
        let root = Path::new(".");
        // 字段不存在 → 空串
        assert_eq!(
            render_template("[{{state.nonexistent}}]", &state, root),
            "[]"
        );
        // 中间节点缺失也应当返回空串
        assert_eq!(render_template("[{{state.user.name}}]", &state, root), "[]");
    }

    #[test]
    fn test_render_path() {
        // 用 tempfile 建一个临时目录 + 文件，project_root 指向临时目录
        let dir = tempfile::tempdir().expect("create tempdir");
        let file_path = dir.path().join("persona.md");
        let mut f = fs::File::create(&file_path).expect("create file");
        f.write_all(b"hello persona").expect("write");
        drop(f);

        let out = render_template("{{path:persona.md}}", &json!({}), dir.path());
        assert_eq!(out, "hello persona");
    }

    #[test]
    fn test_render_path_missing() {
        let dir = tempfile::tempdir().expect("create tempdir");
        let out = render_template("[{{path:does/not/exist.md}}]", &json!({}), dir.path());
        assert_eq!(out, "[]");
    }

    #[test]
    fn test_render_path_read_failure_degrades_to_empty() {
        // 注入确定性读失败（区别于 NotFound）：路径指向目录 → read_to_string 报
        // EISDIR。行为契约：仍降级为空串（warn 仅观测，不影响输出——tracing
        // 输出需 subscriber 捕获，不在单测断言范围）。
        let dir = tempfile::tempdir().expect("create tempdir");
        let sub = dir.path().join("a_dir");
        fs::create_dir(&sub).expect("create dir");
        let out = render_template("[{{path:a_dir}}]", &json!({}), dir.path());
        assert_eq!(out, "[]", "读失败（非 NotFound）同样降级为空串");
    }

    #[test]
    fn test_render_no_template() {
        let state = json!({});
        let root = Path::new(".");
        let src = "just plain text, nothing to replace";
        assert_eq!(render_template(src, &state, root), src);
    }

    #[test]
    fn test_render_value_recursive() {
        let state = json!({ "agent_id": "A1", "count": 5 });
        let root = Path::new(".");
        let input = json!({
            "name": "{{state.agent_id}}",
            "nested": {
                "deep": "n={{state.agent_id}}",
                "missing": "[{{state.nope}}]",
                "num": 42,
                "bool": true,
                "null": null
            },
            "list": [
                "{{state.agent_id}}",
                { "inner": "{{state.count}}" },
                "plain"
            ]
        });
        let out = render_value(&input, &state, root);
        assert_eq!(out["name"], json!("A1"));
        assert_eq!(out["nested"]["deep"], json!("n=A1"));
        assert_eq!(out["nested"]["missing"], json!("[]"));
        // 非字符串标量保持原样
        assert_eq!(out["nested"]["num"], json!(42));
        assert_eq!(out["nested"]["bool"], json!(true));
        assert_eq!(out["nested"]["null"], Value::Null);
        // 数组递归
        assert_eq!(out["list"][0], json!("A1"));
        assert_eq!(out["list"][1]["inner"], json!("5"));
        assert_eq!(out["list"][2], json!("plain"));
    }

    #[test]
    fn test_render_mixed() {
        let dir = tempfile::tempdir().expect("create tempdir");
        let file_path = dir.path().join("x.txt");
        fs::write(&file_path, "FILE").expect("write");

        let state = json!({ "agent_id": "ag-1" });
        let src = "agent={{state.agent_id}}, file={{path:x.txt}}, miss=[{{state.none}}]";
        let out = render_template(src, &state, dir.path());
        assert_eq!(out, "agent=ag-1, file=FILE, miss=[]");
    }

    #[test]
    fn test_render_unknown_expr_literal() {
        // 未识别前缀 → 原样保留
        let state = json!({});
        let root = Path::new(".");
        assert_eq!(
            render_template("v={{foobar.baz}}", &state, root),
            "v={{foobar.baz}}"
        );
    }

    #[test]
    fn test_render_multiple_templates() {
        let state = json!({ "a": "1", "b": "2" });
        let root = Path::new(".");
        let out = render_template("{{state.a}}-{{state.b}}-{{state.a}}", &state, root);
        assert_eq!(out, "1-2-1");
    }

    #[test]
    fn test_render_utf8_safe() {
        // 确保多字节字符不被破坏
        let state = json!({ "name": "你好" });
        let root = Path::new(".");
        let out = render_template("世界, {{state.name}}!", &state, root);
        assert_eq!(out, "世界, 你好!");
    }
}
