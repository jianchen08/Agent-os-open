//! YAML 目录递归扫描（config_center / plugin-loader 共用骨架）。
//!
//! 目录遍历、隐藏文件跳过、yaml/yml 扩展名过滤、stem 键名、空子目录折叠——
//! 这套骨架在 config crate（`load_dir`）与 plugin-loader crate（插件配置注入）
//! 曾是两份近逐字副本且已漂移，收敛于此；单文件如何读取/解析（缓存、
//! warn+跳过 vs 读失败传播）属调用方语义，经 `load_file` 闭包注入。

use std::path::Path;

/// 递归收集目录下 yaml/yml 文件到 `config_map`。
///
/// - 文件：以 stem（不含扩展名）为 key，`load_file` 返回的 Value 为 value；
///   `load_file` 返回 `Ok(None)` 表示调用方已告警并跳过该文件，`Err` 原样传播。
/// - 子目录：递归收集后以目录名为 key 收录（空子目录折叠、不产生空对象）。
/// - 跳过：`.` 前缀隐藏文件、非 yaml/yml 扩展名。
/// - 目录不可读：经 `read_dir_error` 映射为调用方错误类型（保留触发目录，
///   逐层映射与原各自实现的消息逐字一致）。
pub fn collect_yaml_dir<F, G, E>(
    dir: &Path,
    config_map: &mut serde_json::Map<String, serde_json::Value>,
    load_file: &mut F,
    read_dir_error: &mut G,
) -> Result<(), E>
where
    F: FnMut(&Path) -> Result<Option<serde_json::Value>, E>,
    G: FnMut(&Path, std::io::Error) -> E,
{
    let entries = std::fs::read_dir(dir).map_err(|e| read_dir_error(dir, e))?;

    for entry in entries.flatten() {
        let path = entry.path();

        if path.is_dir() {
            let dir_name = path
                .file_name()
                .map(|n| n.to_string_lossy().to_string())
                .unwrap_or_default();
            let mut sub_map = serde_json::Map::new();
            collect_yaml_dir(&path, &mut sub_map, load_file, read_dir_error)?;
            if !sub_map.is_empty() {
                config_map.insert(dir_name, serde_json::Value::Object(sub_map));
            }
        } else if path.is_file() {
            // 跳过隐藏文件（`.` 前缀，Unix 惯例的元数据/规范文档）
            let is_hidden = path
                .file_name()
                .map(|n| n.to_string_lossy().starts_with('.'))
                .unwrap_or(false);
            if is_hidden {
                continue;
            }
            // 只处理 yaml/yml
            let ext = path.extension().map(|e| e.to_string_lossy().to_string());
            if ext.as_deref() != Some("yaml") && ext.as_deref() != Some("yml") {
                continue;
            }
            let stem = path
                .file_stem()
                .map(|n| n.to_string_lossy().to_string())
                .unwrap_or_default();
            match load_file(&path) {
                Ok(Some(value)) => {
                    config_map.insert(stem, value);
                }
                Ok(None) => {}
                Err(e) => return Err(e),
            }
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;

    /// 建目录写文件：相对 `root` 建父目录后写入内容。
    fn write(root: &Path, rel: &str, content: &str) {
        let p = root.join(rel);
        fs::create_dir_all(p.parent().unwrap()).unwrap();
        fs::write(p, content).unwrap();
    }

    /// 测试用单文件装载：读文本，字节内容即 JSON 字符串值；
    /// 文件名含 "boom" 时返回自定义错误（错误传播路径）。
    fn string_loader(path: &Path) -> Result<Option<serde_json::Value>, String> {
        let name = path.file_name().unwrap().to_string_lossy().to_string();
        if name.contains("boom") {
            return Err(format!("io failure: {name}"));
        }
        let content = fs::read_to_string(path).unwrap();
        Ok(Some(serde_json::Value::String(content)))
    }

    #[test]
    fn nests_subdirs_and_keys_by_stem() {
        let tmp = tempfile::tempdir().unwrap();
        let root = tmp.path();
        write(root, "a.yaml", "A");
        write(root, "b.yml", "B");
        write(root, "sub/c.yaml", "C");
        write(root, "sub/deep/d.yaml", "D");

        let mut map = serde_json::Map::new();
        let mut read_dir_error = |_: &Path, e: std::io::Error| e.to_string();
        collect_yaml_dir(root, &mut map, &mut string_loader, &mut read_dir_error).unwrap();

        assert_eq!(map.get("a"), Some(&serde_json::json!("A")));
        assert_eq!(map.get("b"), Some(&serde_json::json!("B")));
        assert_eq!(
            map.get("sub"),
            Some(&serde_json::json!({"c": "C", "deep": {"d": "D"}}))
        );
    }

    #[test]
    fn skips_hidden_and_non_yaml_files() {
        let tmp = tempfile::tempdir().unwrap();
        let root = tmp.path();
        write(root, ".hidden.yaml", "H");
        write(root, "notes.txt", "T");
        write(root, "spec.json", "J");
        write(root, "plainyaml", "no-ext");

        let mut map = serde_json::Map::new();
        let mut read_dir_error = |_: &Path, e: std::io::Error| e.to_string();
        collect_yaml_dir(root, &mut map, &mut string_loader, &mut read_dir_error).unwrap();

        assert!(map.is_empty(), "隐藏/非 yaml 文件不应收录，实际: {map:?}");
    }

    #[test]
    fn empty_subdir_is_folded_away() {
        let tmp = tempfile::tempdir().unwrap();
        let root = tmp.path();
        fs::create_dir_all(root.join("empty")).unwrap();
        write(root, "a.yaml", "A");

        let mut map = serde_json::Map::new();
        let mut read_dir_error = |_: &Path, e: std::io::Error| e.to_string();
        collect_yaml_dir(root, &mut map, &mut string_loader, &mut read_dir_error).unwrap();

        assert_eq!(map.get("a"), Some(&serde_json::json!("A")));
        assert!(!map.contains_key("empty"), "空子目录不应产生空对象");
    }

    #[test]
    fn load_file_none_skips_without_entry() {
        let tmp = tempfile::tempdir().unwrap();
        let root = tmp.path();
        write(root, "skipme.yaml", "S");
        write(root, "keep.yaml", "K");

        let mut map = serde_json::Map::new();
        let mut loader = |p: &Path| {
            let name = p.file_name().unwrap().to_string_lossy().to_string();
            if name.starts_with("skip") {
                return Ok(None); // 调用方已告警跳过
            }
            string_loader(p)
        };
        let mut read_dir_error = |_: &Path, e: std::io::Error| e.to_string();
        collect_yaml_dir(root, &mut map, &mut loader, &mut read_dir_error).unwrap();

        assert!(map.contains_key("keep"));
        assert!(!map.contains_key("skipme"));
    }

    #[test]
    fn load_file_error_propagates_and_aborts() {
        let tmp = tempfile::tempdir().unwrap();
        let root = tmp.path();
        write(root, "ok.yaml", "A");
        write(root, "sub/boom.yaml", "B");

        let mut map = serde_json::Map::new();
        let mut loader = string_loader;
        let mut read_dir_error = |_: &Path, e: std::io::Error| e.to_string();
        let err = collect_yaml_dir(root, &mut map, &mut loader, &mut read_dir_error)
            .expect_err("文件级 Err 应传播");
        assert!(err.contains("boom"), "错误应保留根因: {err}");
        assert!(!map.values().any(|v| v == "B"), "出错的文件自身不得被收录");
    }

    #[test]
    fn missing_dir_maps_via_read_dir_error() {
        let tmp = tempfile::tempdir().unwrap();
        let missing = tmp.path().join("no-such-dir");

        let mut map = serde_json::Map::new();
        let mut loader = string_loader;
        let mut read_dir_error =
            |dir: &Path, e: std::io::Error| format!("read failed at {}: {}", dir.display(), e);
        let err = collect_yaml_dir(&missing, &mut map, &mut loader, &mut read_dir_error)
            .expect_err("目录不存在应映射为调用方错误");
        assert!(err.contains("no-such-dir"), "映射应保留触发目录: {err}");
    }
}
