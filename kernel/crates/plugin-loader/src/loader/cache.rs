// @feature: FP-0.2.一 插件协议（发现面） | @ci: rust-test
//! 插件发现缓存（T4/R-2 层 2「不变不重来」，2026-11-01）。
//!
//! 打包期（deploy/materialize_seed.py --bake-discovery-cache）把插件面逐文件
//! SHA-256 与候选表预烤进种子（`<AGENTOS_PLUGINS_DIR>/discovery-cache.json`，
//! env [`DISCOVERY_CACHE_ENV`] 可覆盖）；boot 专用的
//! [`PluginLoaderImpl::discover_boot_cached`] 先对账：面内全部文件摘要与磁盘
//! 逐字节对上 → 候选直接出自走查在手的 manifest 字节（跳过现场扫描重解），
//! 胜者归属缓存已验证者透出 [`DiscoveryCacheOutcome`] 供 G2 boot 免 spawn
//! 复验；任一不匹配/缓存缺席/版本不识别 → 整体回退现行全量路径（漂移照常）。
//! 缓存只加速冷启动一致态，热发现 watcher 走 `discover` 原路不受影响。
//!
//! 摘要口径（2026-11-20 启动优化 A 扩展，两侧同律）：运行期/开发期写入面
//! 目录（`__pycache__`/`.venv*`/`.audit_tmp`/`data`/`tests`，见
//! [`is_runtime_dir_name`]）整棵子树在摘要面外——装机面此类文件可达数十万
//! （实测 20 万文件 200s 全爬），排除后 boot 走查只对源码面字节。真实源码
//! （plugin.json/*.py 等）仍在摘要面，变更照常打穿；烘焙旧缓存残留的运行期
//! 条目按同律豁免（存量装机免重烘焙即命中）。
//!
//! 格式契约（与 materialize_seed.py `build_discovery_cache` 字节对齐）见
//! docs/working/移动版精细测试_台账.md「T4 第一刀」设计节。

use std::collections::{HashMap, HashSet};
use std::path::{Path, PathBuf};

use agentos_core::traits::{PluginLoader, PluginManifest};
use serde::Deserialize;
use sha2::{Digest, Sha256};
use tracing::{info, warn};

use super::PluginLoaderImpl;

/// 缓存工件路径环境变量覆盖（缺省 = `<内置根>/discovery-cache.json`）。
pub const DISCOVERY_CACHE_ENV: &str = "AGENTOS_DISCOVERY_CACHE";

/// 缓存工件文件名（与烘焙侧 CACHE_ENTRY 尾段一致）。
const DISCOVERY_CACHE_FILENAME: &str = "discovery-cache.json";

/// 只识别此版本；其余（更高/更低/缺失）一律回退全量路径（防格式漂移静默错读）。
const DISCOVERY_CACHE_VERSION: u64 = 1;

/// 可派生 Python 工件（T4 设备轮实证排除，两侧同律）：解释器运行期在插件面内
/// 写 `__pycache__/` 字节码缓存，boot 间即新于缓存——非漂移，不入摘要不比对。
const PYCACHE_DIR: &str = "__pycache__";
const PYC_EXT: &str = ".pyc";

/// 运行期/开发期写入面目录排除（2026-11-20 启动优化 A，两侧同律——烘焙侧
/// materialize_seed.py 与 overlay_installed_update.py 同规则；ADR 草稿
/// docs/working/2026-11-20-discovery-digest-runtime-exclusion_ADR草稿.md）：
/// 这些目录整棵子树由运行期（venv 供给/审计暂存/插件数据）或开发期（tests）
/// 写入，非插件运行语义载体，装机面可达数十万文件——不入摘要不比对不判
/// unexpected。摘要契约 = 「缓存候选对运行语义字节可信」，非「面字节冻结」：
/// 真实源码（plugin.json/*.py 等，不在排除目录内）仍在摘要面，变更照常打穿。
/// 烘焙旧缓存残留的此类条目按同律豁免（完整性对账跳过），存量装机免重烘焙
/// 即得提速。目录名单边新增 = 混布期安全回退全量，无错读风险。
const AUDIT_TMP_DIR: &str = ".audit_tmp";
const VENV_DIR_PREFIX: &str = ".venv";
const RUNTIME_DATA_DIR: &str = "data";
const TESTS_DIR: &str = "tests";

/// 目录段是否属于运行期/开发期写入面（摘要在面外）。
fn is_runtime_dir_name(name: &str) -> bool {
    name == PYCACHE_DIR
        || name.starts_with(VENV_DIR_PREFIX)
        || name == AUDIT_TMP_DIR
        || name == RUNTIME_DATA_DIR
        || name == TESTS_DIR
}

/// 缓存键（相对路径）是否在摘要面外：任一路径段命中 [`is_runtime_dir_name`]，
/// 或尾段为 `.pyc`。完整性对账用它豁免旧缓存残留的运行期条目。
fn is_runtime_face_path(rel: &str) -> bool {
    let mut last = "";
    for seg in rel.split('/') {
        if is_runtime_dir_name(seg) {
            return true;
        }
        last = seg;
    }
    last.ends_with(PYC_EXT)
}

/// 候选流条目：(manifest, manifest 路径, 是否出自缓存已验证字节)。
pub(super) type DiscoverCandidate = (PluginManifest, PathBuf, bool);

/// 缓存工件（serde 默认宽容缺字段——缺关键字段走对账失败回退，不 panic）。
#[derive(Debug, Deserialize)]
struct DiscoveryCacheFile {
    version: u64,
    #[serde(default)]
    face_root: String,
    #[serde(default)]
    files: HashMap<String, String>,
    #[serde(default)]
    links: HashMap<String, String>,
    #[serde(default)]
    plugin_dirs: HashMap<String, HashMap<String, String>>,
}

/// boot 发现缓存消费结果。
///
/// `cache_verified_winner_ids` = 同 id 裁决胜者的候选字节出自缓存已验证内置面
/// 的插件 id——G2 boot 据此免 spawn/observe 复验（胜者为用户副本者不在集内，
/// 照常复验）。`hit=false` 时为空集。
#[derive(Debug, Default, Clone)]
pub struct DiscoveryCacheOutcome {
    pub hit: bool,
    pub cache_verified_winner_ids: HashSet<String>,
}

impl PluginLoaderImpl {
    /// 缓存工件路径：env 覆盖 > `<内置根>/discovery-cache.json`。
    pub(crate) fn discovery_cache_path(&self) -> PathBuf {
        std::env::var(DISCOVERY_CACHE_ENV)
            .map(PathBuf::from)
            .unwrap_or_else(|_| self.builtin_root.join(DISCOVERY_CACHE_FILENAME))
    }

    /// boot 专用发现入口：先探测预烤发现缓存（T4/R-2 层 2「不变不重来」）。
    ///
    /// 缓存对账全匹配 → 候选直接出自缓存已验证字节（跳过现场扫描重解），胜者
    /// 中缓存已验证者随 [`DiscoveryCacheOutcome`] 透出供 G2 boot 免 spawn 复验；
    /// 任一不匹配/缓存缺席/版本不识别 → 整体回退现行全量路径（漂移照常，热
    /// 发现 watcher 走 [`PluginLoader::discover`] 不受影响——缓存只加速冷启动
    /// 一致态）。契约冻结面：[`PluginLoader::discover`] 签名与语义零变化。
    pub async fn discover_boot_cached(
        &self,
        root_paths: &[&str],
    ) -> Result<(Vec<PluginManifest>, DiscoveryCacheOutcome), agentos_core::types::PluginError>
    {
        self.sync_external_mcp_plugins();
        match self.gather_candidates_via_cache(root_paths) {
            Ok(candidates) => {
                let (manifests, verified) = self.finalize_discovery(candidates)?;
                info!(
                    target: "plugin-loader",
                    plugins = manifests.len(),
                    cache_verified_winners = verified.len(),
                    "discovery cache hit: boot scan + dual-source re-resolve bypassed (prebaked face digests all matched)"
                );
                Ok((
                    manifests,
                    DiscoveryCacheOutcome {
                        hit: true,
                        cache_verified_winner_ids: verified,
                    },
                ))
            }
            Err(reason) => {
                info!(
                    target: "plugin-loader",
                    reason = %reason,
                    "discovery cache not applicable: full discovery path this boot"
                );
                let manifests = self.discover(root_paths).await?;
                Ok((manifests, DiscoveryCacheOutcome::default()))
            }
        }
    }

    /// 缓存对账出候选流。Err(原因) = 回退全量路径（缺席/版本不识别/任一摘要
    /// 不匹配/manifest 解析校验失败）。
    fn gather_candidates_via_cache(
        &self,
        root_paths: &[&str],
    ) -> Result<Vec<DiscoverCandidate>, String> {
        let cache_path = self.discovery_cache_path();
        let raw = std::fs::read(&cache_path)
            .map_err(|e| format!("cache unreadable at {}: {e}", cache_path.display()))?;
        let cache: DiscoveryCacheFile =
            serde_json::from_slice(&raw).map_err(|e| format!("cache parse failed: {e}"))?;
        if cache.version != DISCOVERY_CACHE_VERSION {
            return Err(format!(
                "cache version {} not recognized (want {DISCOVERY_CACHE_VERSION})",
                cache.version
            ));
        }
        let face_root = PathBuf::from(&cache.face_root);
        if !face_root.is_dir() {
            return Err(format!("face_root {:?} not a directory", cache.face_root));
        }
        // 候选 manifest 绝对路径集：走查时按路径捕获字节（解析零二次读盘）。
        let mut wanted: HashSet<PathBuf> = HashSet::new();
        for (root_str, dirs) in &cache.plugin_dirs {
            for (dir, manifest) in dirs {
                wanted.insert(Path::new(root_str).join(dir).join(manifest));
            }
        }
        let mut manifest_bytes: HashMap<PathBuf, Vec<u8>> = HashMap::new();
        let mut seen_files: HashSet<String> = HashSet::new();
        let mut seen_links: HashSet<String> = HashSet::new();
        walk_face(
            &face_root,
            &cache,
            &cache_path,
            "",
            &wanted,
            &mut manifest_bytes,
            &mut seen_files,
            &mut seen_links,
        )?;
        // 覆盖完备性：磁盘走查到的条目可能少于缓存（缓存登记而磁盘缺席）。
        // 摘要面外的键（运行期/开发期写入面，2026-11-20 口径扩展）按同律豁免——
        // 烘焙旧缓存残留的 .venv*/data/tests 等条目不构成缺席漂移，存量装机
        // 免重烘焙即命中（新口径烘焙侧同律不再登记此类条目）。
        for key in cache.files.keys() {
            if is_runtime_face_path(key) {
                continue;
            }
            if !seen_files.contains(key) {
                return Err(format!("face file missing on disk: {key}"));
            }
        }
        for key in cache.links.keys() {
            if is_runtime_face_path(key) {
                continue;
            }
            if !seen_links.contains(key) {
                return Err(format!("face link missing on disk: {key}"));
            }
        }
        // 逐根出候选：face 内根 → 缓存候选；face 外根（用户面，模式播种/用户
        // 副本天然动态）→ 现行 scan_root 现扫。根序与 gather_candidates_live
        // 一致（root_paths → 内置根 → 用户根），admit 裁决语义依赖此序。
        let mut candidates = Vec::new();
        for root_str in root_paths {
            push_root_candidates(
                self,
                &cache,
                &face_root,
                &manifest_bytes,
                Path::new(root_str),
                &mut candidates,
            )?;
        }
        push_root_candidates(
            self,
            &cache,
            &face_root,
            &manifest_bytes,
            &self.builtin_root,
            &mut candidates,
        )?;
        if let Some(user_root) = &self.user_root {
            push_root_candidates(
                self,
                &cache,
                &face_root,
                &manifest_bytes,
                user_root,
                &mut candidates,
            )?;
        }
        Ok(candidates)
    }

    /// 缓存 manifest 字节解析+校验（与 scan_root 同款 json→yaml 兜底 + 内部
    /// 校验）；任何失败 = 回退全量路径（全量路径对同字节会走一致的跳过+warn）。
    fn parse_cached_manifest(&self, content: &[u8], path: &Path) -> Result<PluginManifest, String> {
        let text = String::from_utf8(content.to_vec())
            .map_err(|e| format!("manifest not utf-8 {}: {e}", path.display()))?;
        let mut manifest: PluginManifest = match serde_json::from_str(&text) {
            Ok(m) => m,
            Err(json_err) => match serde_yaml::from_str(&text) {
                Ok(m) => m,
                Err(yaml_err) => {
                    return Err(format!(
                        "manifest parse failed {}: json: {json_err}; yaml: {yaml_err}",
                        path.display()
                    ));
                }
            },
        };
        self.validate_manifest_internal(&mut manifest, path)
            .map_err(|e| e.to_string())?;
        Ok(manifest)
    }
}

/// 单根出候选：face 内根查 `plugin_dirs`（缓存候选，manifest 字节在手解析；
/// 目录序确定性排序），face 外根现行扫描（失败 warn 不阻断，与 live 同款）。
fn push_root_candidates(
    loader: &PluginLoaderImpl,
    cache: &DiscoveryCacheFile,
    face_root: &Path,
    manifest_bytes: &HashMap<PathBuf, Vec<u8>>,
    root: &Path,
    out: &mut Vec<DiscoverCandidate>,
) -> Result<(), String> {
    if !root.starts_with(face_root) {
        match loader.scan_root(root) {
            Ok(found) => {
                for (manifest, path) in found {
                    out.push((manifest, path, false));
                }
            }
            Err(e) => warn!("Failed to scan root {}: {}", root.display(), e),
        }
        return Ok(());
    }
    let root_str = root.to_string_lossy().to_string();
    let Some(dirs) = cache.plugin_dirs.get(&root_str) else {
        // face 内根而无候选表：面摘要已证 manifest 集与烘焙一致 ⇒ 该根零候选
        // 与全量路径扫描结果一致（多出的 manifest 文件早在走查被判漂移）。
        return Ok(());
    };
    let mut dir_names: Vec<&String> = dirs.keys().collect();
    dir_names.sort();
    for dir in dir_names {
        let manifest_name = &dirs[dir];
        let manifest_path = root.join(dir).join(manifest_name);
        let bytes = manifest_bytes.get(&manifest_path).ok_or_else(|| {
            format!(
                "manifest bytes not captured during face walk: {}",
                manifest_path.display()
            )
        })?;
        let manifest = loader.parse_cached_manifest(bytes, &manifest_path)?;
        out.push((manifest, manifest_path, true));
    }
    Ok(())
}

/// 插件面递归走查：常规文件逐个 sha256 对照 `files`；符号链接 read_link 对照
/// `links`；磁盘多出条目即漂移。缓存工件本体自指排除；运行期/开发期写入面
/// （`__pycache__/`、`.venv*`、`.audit_tmp/`、`data/`、`tests/` 目录整棵子树
/// 与 `*.pyc` 文件，[`is_runtime_dir_name`]）同排除——非漂移不比对不判
/// unexpected，两侧同律（烘焙侧 materialize_seed.py / overlay_installed_update.py
/// 同规则），真实源码变更仍在摘要面照常打穿。rel 路径恒 `/` 分隔（与烘焙侧
/// POSIX 键对齐；Windows 反斜杠只在 dev 出现，缓存面根不存在 → 天然回退）。
/// manifest 候选字节按 `wanted` 捕获。
#[allow(clippy::too_many_arguments)]
fn walk_face(
    dir: &Path,
    cache: &DiscoveryCacheFile,
    cache_artifact: &Path,
    rel: &str,
    wanted: &HashSet<PathBuf>,
    manifest_bytes: &mut HashMap<PathBuf, Vec<u8>>,
    seen_files: &mut HashSet<String>,
    seen_links: &mut HashSet<String>,
) -> Result<(), String> {
    let entries = std::fs::read_dir(dir).map_err(|e| format!("read_dir {}: {e}", dir.display()))?;
    for entry in entries.flatten() {
        let child = entry.path();
        let name = entry.file_name().to_string_lossy().to_string();
        let child_rel = if rel.is_empty() {
            name.clone()
        } else {
            format!("{rel}/{name}")
        };
        if child == cache_artifact {
            continue; // 缓存工件本体（自指排除，烘焙侧同样不入摘要）
        }
        let ft = entry
            .file_type()
            .map_err(|e| format!("file_type {}: {e}", child.display()))?;
        // 运行期/开发期写入面排除（T4 pyc 实证 + 2026-11-20 口径扩展）：目录
        // 整棵子树不入摘要不比对不判 unexpected（venv 供给/审计暂存/插件数据/
        // tests 均非运行语义载体）；`*.pyc` 文件同理。两侧同律，源码变更照常
        // 打穿。
        if ft.is_dir() && is_runtime_dir_name(&name) {
            continue;
        }
        if ft.is_file() && name.ends_with(PYC_EXT) {
            continue;
        }
        if ft.is_dir() {
            walk_face(
                &child,
                cache,
                cache_artifact,
                &child_rel,
                wanted,
                manifest_bytes,
                seen_files,
                seen_links,
            )?;
        } else if ft.is_symlink() {
            let target = std::fs::read_link(&child)
                .map_err(|e| format!("read_link {}: {e}", child.display()))?
                .to_string_lossy()
                .replace('\\', "/");
            let want = cache
                .links
                .get(&child_rel)
                .ok_or_else(|| format!("unexpected face symlink: {child_rel}"))?;
            if *want != target {
                return Err(format!(
                    "symlink drift: {child_rel}: disk={target} cache={want}"
                ));
            }
            seen_links.insert(child_rel);
        } else if ft.is_file() {
            let data =
                std::fs::read(&child).map_err(|e| format!("read {}: {e}", child.display()))?;
            let digest = sha256_hex(&data);
            let want = cache
                .files
                .get(&child_rel)
                .ok_or_else(|| format!("unexpected face file: {child_rel}"))?;
            if *want != digest {
                return Err(format!("digest drift: {child_rel}"));
            }
            seen_files.insert(child_rel);
            let abs = dir.join(&name);
            if wanted.contains(&abs) {
                manifest_bytes.insert(abs, data);
            }
        } else {
            return Err(format!("unsupported face entry type: {child_rel}"));
        }
    }
    Ok(())
}

fn sha256_hex(data: &[u8]) -> String {
    let mut h = Sha256::new();
    h.update(data);
    h.finalize().iter().map(|b| format!("{b:02x}")).collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;

    /// 迷你插件面：两个插件（manifest + 代码文件 + 嵌套子文件），布局与种子
    /// 平铺一致（`<root>/shared/<id>/plugin.json`）。
    fn write_face(base: &Path) -> PathBuf {
        let plugins = base.join("opt/agentos/plugins");
        let llm = plugins.join("shared/llm");
        fs::create_dir_all(&llm).unwrap();
        fs::write(
            llm.join("plugin.json"),
            r#"{"id":"llm_service","name":"Test llm","version":"0.1.0","plugin_type":"tool","language":"python","host_type":"sidecar","entry":"python server.py","capabilities":{}}"#,
        )
        .unwrap();
        fs::write(llm.join("server.py"), b"print('llm')\n").unwrap();
        let core = plugins.join("shared/llm_core");
        fs::create_dir_all(core.join("sub")).unwrap();
        fs::write(
            core.join("plugin.json"),
            r#"{"id":"pipeline_llm_core","name":"Test llm_core","version":"0.2.0","plugin_type":"pipeline","language":"python","host_type":"sidecar","entry":"python server.py","capabilities":{},"invoke_entry":"pipeline_llm_core.execute"}"#,
        )
        .unwrap();
        fs::write(core.join("sub/deep.py"), b"DEEP=42\n").unwrap();
        plugins
    }

    /// 与烘焙侧同构的缓存字节（测试手写；file 键与走查侧同为 `/` 分隔）。
    /// `legacy_shape=true` = 2026-11-20 前旧烘焙口径（只排 `__pycache__`/`*.pyc`，
    /// `.venv*`/`data`/`tests` 条目照常登记——存量装机缓存形态）；缺省新口径
    /// 与走查/烘焙两侧同律排除（[`is_runtime_dir_name`]）。
    fn cache_bytes_shape(
        plugins_root: &Path,
        legacy_shape: bool,
        mutate: impl FnOnce(&mut serde_json::Value),
    ) -> Vec<u8> {
        let sha = |p: &Path| sha256_hex(&fs::read(p).unwrap());
        let root_str = plugins_root.to_string_lossy().to_string();
        let face_cut = root_str.len() + 1;
        let mut files = serde_json::Map::new();
        let mut stack = vec![plugins_root.to_path_buf()];
        while let Some(dir) = stack.pop() {
            for entry in fs::read_dir(&dir).unwrap().flatten() {
                let p = entry.path();
                let name = entry.file_name().to_string_lossy().to_string();
                if p.is_dir() {
                    let excluded = if legacy_shape {
                        name == PYCACHE_DIR
                    } else {
                        is_runtime_dir_name(&name)
                    };
                    if excluded {
                        continue;
                    }
                    stack.push(p);
                } else if name.ends_with(PYC_EXT) {
                    continue;
                } else {
                    let rel = p.to_string_lossy()[face_cut..].replace('\\', "/");
                    files.insert(rel, serde_json::Value::String(sha(&p)));
                }
            }
        }
        let mut v = serde_json::json!({
            "version": 1u64,
            "generator": "test",
            "face_root": root_str,
            "files": files,
            "links": {},
            "plugin_dirs": {
                plugins_root.join("shared").to_string_lossy(): {
                    "llm": "plugin.json",
                    "llm_core": "plugin.json",
                }
            }
        });
        mutate(&mut v);
        (serde_json::to_string(&v).unwrap() + "\n").into_bytes()
    }

    fn cache_bytes(plugins_root: &Path, mutate: impl FnOnce(&mut serde_json::Value)) -> Vec<u8> {
        cache_bytes_shape(plugins_root, false, mutate)
    }

    fn write_cache(plugins_root: &Path, mutate: impl FnOnce(&mut serde_json::Value)) {
        fs::write(
            plugins_root.join("shared/discovery-cache.json"),
            cache_bytes(plugins_root, mutate),
        )
        .unwrap();
    }

    /// 旧烘焙口径缓存（登记 `.venv*`/`data`/`tests` 条目，存量装机形态）。
    fn write_legacy_cache(plugins_root: &Path, mutate: impl FnOnce(&mut serde_json::Value)) {
        fs::write(
            plugins_root.join("shared/discovery-cache.json"),
            cache_bytes_shape(plugins_root, true, mutate),
        )
        .unwrap();
    }

    /// 给迷你插件面加运行期/开发期写入树（装机实证形态：venv 供给、审计暂存、
    /// 插件运行数据、tests 开发面；`.venv-x` 验证前缀匹配非精确名）。
    fn add_runtime_trees(plugins: &Path) {
        fs::create_dir_all(plugins.join("shared/llm/.venv-x/bin")).unwrap();
        fs::write(plugins.join("shared/llm/.venv-x/bin/python.exe"), b"PYEXE").unwrap();
        fs::create_dir_all(plugins.join("shared/llm/__pycache__")).unwrap();
        fs::write(
            plugins.join("shared/llm/__pycache__/server.cpython-312.pyc"),
            b"\x00PYC",
        )
        .unwrap();
        fs::create_dir_all(plugins.join("shared/_host/.audit_tmp")).unwrap();
        fs::write(
            plugins.join("shared/_host/.audit_tmp/audit_ledger.json"),
            b"{}",
        )
        .unwrap();
        fs::create_dir_all(plugins.join("shared/llm_core/data/kb")).unwrap();
        fs::write(plugins.join("shared/llm_core/data/kb/chunk.bin"), b"KB").unwrap();
        fs::create_dir_all(plugins.join("shared/llm/tests")).unwrap();
        fs::write(
            plugins.join("shared/llm/tests/test_llm.py"),
            b"def test_x(): pass\n",
        )
        .unwrap();
        fs::create_dir_all(plugins.join("shared/data")).unwrap();
        fs::write(plugins.join("shared/data/permission_modes.json"), b"{}").unwrap();
    }

    fn boot_roots(plugins_root: &Path) -> Vec<String> {
        vec![plugins_root.join("shared").to_string_lossy().to_string()]
    }

    fn as_refs(roots: &[String]) -> Vec<&str> {
        roots.iter().map(|s| s.as_str()).collect()
    }

    fn sorted_ids(ms: &[PluginManifest]) -> Vec<String> {
        let mut v: Vec<String> = ms.iter().map(|m| m.id.clone()).collect();
        v.sort();
        v
    }

    /// 一致态短路（分支 1）：缓存命中，manifests 与全量路径一致，胜者全部入
    /// 缓存已验证集，收口副作用（manifests 路径缓存）与全量路径同构。
    #[tokio::test]
    async fn cache_hit_short_circuits_and_matches_full_path() {
        let _env_lock = crate::test_env::user_space_env_lock();
        let tmp = tempfile::tempdir().unwrap();
        let plugins = write_face(tmp.path());
        write_cache(&plugins, |_| {});
        let loader = PluginLoaderImpl::new(plugins.join("shared"), None::<PathBuf>);
        let roots = boot_roots(&plugins);
        let root_refs = as_refs(&roots);

        let (cached, outcome) = loader.discover_boot_cached(&root_refs).await.unwrap();
        assert!(outcome.hit, "一致态必须命中");
        assert_eq!(sorted_ids(&cached).len(), 2);
        let full = loader.discover(&root_refs).await.unwrap();
        assert_eq!(
            sorted_ids(&cached),
            sorted_ids(&full),
            "缓存候选集与全量路径一致"
        );
        assert!(
            outcome.cache_verified_winner_ids.contains("llm_service")
                && outcome
                    .cache_verified_winner_ids
                    .contains("pipeline_llm_core"),
            "内置缓存胜者全部免 G2 复验: {:?}",
            outcome.cache_verified_winner_ids
        );
        assert!(
            loader.manifests.read().contains_key("llm_service"),
            "短路路径同样完成收口副作用（路径缓存重建）"
        );
    }

    /// 单文件漂移回退全量（分支 2）：代码文件（非 manifest）被改 → 走查判漂移
    /// → 回退全量路径，manifests 仍正确，零免验 id。
    #[tokio::test]
    async fn single_file_drift_falls_back_to_full_path() {
        let _env_lock = crate::test_env::user_space_env_lock();
        let tmp = tempfile::tempdir().unwrap();
        let plugins = write_face(tmp.path());
        write_cache(&plugins, |_| {});
        // 漂移：manifest 未动、代码文件原位改写
        fs::write(plugins.join("shared/llm/server.py"), b"print('CHANGED')\n").unwrap();
        let loader = PluginLoaderImpl::new(plugins.join("shared"), None::<PathBuf>);
        let roots = boot_roots(&plugins);
        let root_refs = as_refs(&roots);

        let (manifests, outcome) = loader.discover_boot_cached(&root_refs).await.unwrap();
        assert!(!outcome.hit, "漂移必须回退");
        assert!(outcome.cache_verified_winner_ids.is_empty());
        assert_eq!(manifests.len(), 2, "回退后全量扫描结果不缺");
    }

    /// 缓存缺席回退（分支 3）：无缓存工件 = 现行行为（只增不删）。
    #[tokio::test]
    async fn cache_absent_falls_back_to_full_path() {
        let _env_lock = crate::test_env::user_space_env_lock();
        let tmp = tempfile::tempdir().unwrap();
        let plugins = write_face(tmp.path());
        let loader = PluginLoaderImpl::new(plugins.join("shared"), None::<PathBuf>);
        let roots = boot_roots(&plugins);
        let root_refs = as_refs(&roots);

        let (manifests, outcome) = loader.discover_boot_cached(&root_refs).await.unwrap();
        assert!(!outcome.hit);
        assert_eq!(manifests.len(), 2);
    }

    /// 版本不识别回退（分支 4）：version 字段漂移 → 不许按未知格式错读。
    #[tokio::test]
    async fn unrecognized_version_falls_back_to_full_path() {
        let _env_lock = crate::test_env::user_space_env_lock();
        let tmp = tempfile::tempdir().unwrap();
        let plugins = write_face(tmp.path());
        write_cache(&plugins, |v| {
            v["version"] = serde_json::Value::from(999u64);
        });
        let loader = PluginLoaderImpl::new(plugins.join("shared"), None::<PathBuf>);
        let roots = boot_roots(&plugins);
        let root_refs = as_refs(&roots);

        let (manifests, outcome) = loader.discover_boot_cached(&root_refs).await.unwrap();
        assert!(!outcome.hit, "未知版本必须回退");
        assert_eq!(manifests.len(), 2);
    }

    /// manifest 本体漂移同样回退（层 2 语义：面内任何字节变更都逃不出对账）。
    #[tokio::test]
    async fn manifest_drift_falls_back_to_full_path() {
        let _env_lock = crate::test_env::user_space_env_lock();
        let tmp = tempfile::tempdir().unwrap();
        let plugins = write_face(tmp.path());
        write_cache(&plugins, |_| {});
        fs::write(
            plugins.join("shared/llm/plugin.json"),
            r#"{"id":"llm_service","name":"Test llm","version":"0.2.0","plugin_type":"tool","language":"python","host_type":"sidecar","entry":"python server.py","capabilities":{}}"#,
        )
        .unwrap();
        let loader = PluginLoaderImpl::new(plugins.join("shared"), None::<PathBuf>);
        let roots = boot_roots(&plugins);
        let root_refs = as_refs(&roots);

        let (manifests, outcome) = loader.discover_boot_cached(&root_refs).await.unwrap();
        assert!(!outcome.hit);
        assert_eq!(manifests.len(), 2);
    }

    /// face 外根（用户面）照常现行扫描：缓存命中面内、用户副本动态现扫，
    /// 同 id 归约语义与全量路径一致（version-max 用户 9.0.0 胜 → 不进免验集）。
    #[tokio::test]
    async fn out_of_face_user_root_scanned_live_and_version_max_wins() {
        let _env_lock = crate::test_env::user_space_env_lock();
        let tmp = tempfile::tempdir().unwrap();
        let plugins = write_face(tmp.path());
        write_cache(&plugins, |_| {});
        let user_root = tmp.path().join("user_root/plugins/shared");
        fs::create_dir_all(user_root.join("llm")).unwrap();
        fs::write(
            user_root.join("llm/plugin.json"),
            r#"{"id":"llm_service","name":"Test llm","version":"9.0.0","plugin_type":"tool","language":"python","host_type":"sidecar","entry":"python server.py","capabilities":{}}"#,
        )
        .unwrap();
        let loader = PluginLoaderImpl::new(plugins.join("shared"), Some(user_root.clone()));
        let mut roots = boot_roots(&plugins);
        roots.push(user_root.to_string_lossy().to_string());
        let root_refs = as_refs(&roots);

        let (manifests, outcome) = loader.discover_boot_cached(&root_refs).await.unwrap();
        assert!(outcome.hit, "用户面动态不影响面内命中");
        let llm = manifests.iter().find(|m| m.id == "llm_service").unwrap();
        assert_eq!(
            llm.version, "9.0.0",
            "用户副本 version-max 胜出（与全量路径同归约）"
        );
        assert!(
            !outcome.cache_verified_winner_ids.contains("llm_service"),
            "胜者为用户副本（非缓存已验证字节）→ 照常 G2 复验"
        );
        assert!(outcome
            .cache_verified_winner_ids
            .contains("pipeline_llm_core"));
    }

    /// 面内文件缺席/多出均判漂移回退（覆盖完备性双向）。
    #[tokio::test]
    async fn missing_or_extra_face_file_falls_back() {
        let _env_lock = crate::test_env::user_space_env_lock();
        // 缺席：缓存登记的文件被删
        let tmp = tempfile::tempdir().unwrap();
        let plugins = write_face(tmp.path());
        write_cache(&plugins, |_| {});
        fs::remove_file(plugins.join("shared/llm_core/sub/deep.py")).unwrap();
        let loader = PluginLoaderImpl::new(plugins.join("shared"), None::<PathBuf>);
        let roots = boot_roots(&plugins);
        let root_refs = as_refs(&roots);
        let (_, outcome) = loader.discover_boot_cached(&root_refs).await.unwrap();
        assert!(!outcome.hit, "缓存登记文件磁盘缺席必须回退");

        // 多出：面内新增文件（缓存未登记）
        let tmp2 = tempfile::tempdir().unwrap();
        let plugins2 = write_face(tmp2.path());
        write_cache(&plugins2, |_| {});
        fs::write(plugins2.join("shared/llm/extra.py"), b"x=1\n").unwrap();
        let loader2 = PluginLoaderImpl::new(plugins2.join("shared"), None::<PathBuf>);
        let roots2 = boot_roots(&plugins2);
        let root_refs2 = as_refs(&roots2);
        let (_, outcome2) = loader2.discover_boot_cached(&root_refs2).await.unwrap();
        assert!(!outcome2.hit, "面内多出文件必须回退");
    }

    /// boot 间面内新增可派生工件仍短路（T4 设备轮实证 bug 回归）：boot A 运行期
    /// Python 在插件面写 `__pycache__/*.pyc`（新于缓存）→ 非漂移，boot B 照常
    /// 命中（实测回退形态：verified=6 cache_skipped=0 + 18 条观测失败）。
    #[tokio::test]
    async fn runtime_pycache_artifacts_do_not_invalidate_cache() {
        let _env_lock = crate::test_env::user_space_env_lock();
        let tmp = tempfile::tempdir().unwrap();
        let plugins = write_face(tmp.path());
        write_cache(&plugins, |_| {});
        // 模拟 boot A 运行期写入：嵌套 __pycache__/ 字节码 + 插件目录散落 .pyc
        fs::create_dir_all(plugins.join("shared/llm/__pycache__")).unwrap();
        fs::write(
            plugins.join("shared/llm/__pycache__/server.cpython-312.pyc"),
            b"\x00PYC",
        )
        .unwrap();
        fs::write(plugins.join("shared/llm/loose.pyc"), b"\x00PYC2").unwrap();
        fs::create_dir_all(plugins.join("shared/llm_core/sub/__pycache__")).unwrap();
        fs::write(
            plugins.join("shared/llm_core/sub/__pycache__/deep.cpython-312.pyc"),
            b"\x00PYC3",
        )
        .unwrap();
        let loader = PluginLoaderImpl::new(plugins.join("shared"), None::<PathBuf>);
        let roots = boot_roots(&plugins);
        let root_refs = as_refs(&roots);
        let (manifests, outcome) = loader.discover_boot_cached(&root_refs).await.unwrap();
        assert!(outcome.hit, "运行期 pyc 工件不算漂移：boot B 仍短路");
        assert_eq!(sorted_ids(&manifests).len(), 2);
        assert_eq!(
            outcome.cache_verified_winner_ids.len(),
            2,
            "免验集不受运行期工件影响（cache_skipped 恢复满额）"
        );
    }

    /// 运行期/开发期写入树（2026-11-20 口径扩展） boot 间新增/变更不失效缓存：
    /// `.venv-x/`（venv 供给写、前缀匹配）、`.audit_tmp/`（运行期审计台账，
    /// 2026-11-19 装机 200s 打穿实证）、`data/`（插件运行数据，hindsight 35k
    /// 实证）、`tests/`（开发面，2026-11-20 dsh_adapter 打穿实证）、
    /// `__pycache__/` 在这些树内新增/改写后仍短路，免验集满额。
    #[tokio::test]
    async fn runtime_trees_do_not_invalidate_cache() {
        let _env_lock = crate::test_env::user_space_env_lock();
        let tmp = tempfile::tempdir().unwrap();
        let plugins = write_face(tmp.path());
        add_runtime_trees(&plugins);
        write_cache(&plugins, |_| {});
        // boot 间写入：venv 内新增、审计台账改写、运行数据新增、tests 源修改、
        // pyc 新增——全部落在摘要面外目录
        fs::create_dir_all(plugins.join("shared/llm/.venv-x/lib")).unwrap();
        fs::write(plugins.join("shared/llm/.venv-x/lib/new_pkg.py"), b"x=1\n").unwrap();
        fs::write(
            plugins.join("shared/_host/.audit_tmp/audit_ledger.json"),
            b"{\"n\":2}",
        )
        .unwrap();
        fs::write(plugins.join("shared/llm_core/data/kb/more.bin"), b"KB2").unwrap();
        fs::write(
            plugins.join("shared/llm/tests/test_llm.py"),
            b"def test_y(): pass\n",
        )
        .unwrap();
        fs::write(plugins.join("shared/llm/__pycache__/loose.pyc"), b"\x00P2").unwrap();
        let loader = PluginLoaderImpl::new(plugins.join("shared"), None::<PathBuf>);
        let roots = boot_roots(&plugins);
        let root_refs = as_refs(&roots);
        let (manifests, outcome) = loader.discover_boot_cached(&root_refs).await.unwrap();
        assert!(
            outcome.hit,
            "运行期/开发期写入树的新增与变更不算漂移：boot 仍短路"
        );
        assert_eq!(sorted_ids(&manifests).len(), 2);
        assert_eq!(
            outcome.cache_verified_winner_ids.len(),
            2,
            "免验集不受运行期写入树影响"
        );
    }

    /// 真实源码变更仍打穿（排除口径不许吞源码漂移）：同一含运行期树的面里，
    /// plugin.json 改写 / 插件 .py 改写 / 面内新增源码文件，三者均回退全量。
    #[tokio::test]
    async fn source_changes_still_invalidate_cache_with_runtime_trees() {
        let _env_lock = crate::test_env::user_space_env_lock();
        // plugin.json（manifest）改写
        let tmp = tempfile::tempdir().unwrap();
        let plugins = write_face(tmp.path());
        add_runtime_trees(&plugins);
        write_cache(&plugins, |_| {});
        fs::write(
            plugins.join("shared/llm/plugin.json"),
            r#"{"id":"llm_service","name":"Test llm","version":"0.2.0","plugin_type":"tool","language":"python","host_type":"sidecar","entry":"python server.py","capabilities":{}}"#,
        )
        .unwrap();
        let loader = PluginLoaderImpl::new(plugins.join("shared"), None::<PathBuf>);
        let roots = boot_roots(&plugins);
        let root_refs = as_refs(&roots);
        let (_, outcome) = loader.discover_boot_cached(&root_refs).await.unwrap();
        assert!(!outcome.hit, "manifest 源码变更必须打穿");

        // 插件 .py 源码改写
        let tmp2 = tempfile::tempdir().unwrap();
        let plugins2 = write_face(tmp2.path());
        add_runtime_trees(&plugins2);
        write_cache(&plugins2, |_| {});
        fs::write(plugins2.join("shared/llm_core/sub/deep.py"), b"DEEP=43\n").unwrap();
        let loader2 = PluginLoaderImpl::new(plugins2.join("shared"), None::<PathBuf>);
        let roots2 = boot_roots(&plugins2);
        let root_refs2 = as_refs(&roots2);
        let (manifests2, outcome2) = loader2.discover_boot_cached(&root_refs2).await.unwrap();
        assert!(!outcome2.hit, "插件 .py 源码变更必须打穿");
        assert_eq!(manifests2.len(), 2, "回退后全量扫描结果不缺");

        // 面内新增源码文件（非排除目录内）
        let tmp3 = tempfile::tempdir().unwrap();
        let plugins3 = write_face(tmp3.path());
        add_runtime_trees(&plugins3);
        write_cache(&plugins3, |_| {});
        fs::write(plugins3.join("shared/llm/extra_tool.py"), b"x=1\n").unwrap();
        let loader3 = PluginLoaderImpl::new(plugins3.join("shared"), None::<PathBuf>);
        let roots3 = boot_roots(&plugins3);
        let root_refs3 = as_refs(&roots3);
        let (_, outcome3) = loader3.discover_boot_cached(&root_refs3).await.unwrap();
        assert!(!outcome3.hit, "面内新增源码文件必须打穿");
    }

    /// 存量装机迁移容错（r297 实证形态）：旧烘焙口径缓存登记了 `.venv-x/`、
    /// `data/`、`tests/`、`.audit_tmp/` 条目，且盘上这些文件在烘焙后又改过
    /// （运行期台账重写/测试源同步/数据增长）——新走查按摘要面外同律豁免
    /// （不比对也不判缺席），免重烘焙即命中；同形态下源码漂移照常打穿。
    #[tokio::test]
    async fn legacy_cache_with_runtime_entries_still_hits() {
        let _env_lock = crate::test_env::user_space_env_lock();
        // 旧口径缓存 + 运行期文件烘焙后又变 → 仍命中
        let tmp = tempfile::tempdir().unwrap();
        let plugins = write_face(tmp.path());
        add_runtime_trees(&plugins);
        write_legacy_cache(&plugins, |_| {});
        fs::write(
            plugins.join("shared/_host/.audit_tmp/audit_ledger.json"),
            b"{\"n\":9}",
        )
        .unwrap();
        fs::write(
            plugins.join("shared/llm/tests/test_llm.py"),
            b"def test_z(): pass\n",
        )
        .unwrap();
        fs::write(
            plugins.join("shared/llm_core/data/kb/chunk.bin"),
            b"KB-CHANGED",
        )
        .unwrap();
        let loader = PluginLoaderImpl::new(plugins.join("shared"), None::<PathBuf>);
        let roots = boot_roots(&plugins);
        let root_refs = as_refs(&roots);
        let (manifests, outcome) = loader.discover_boot_cached(&root_refs).await.unwrap();
        assert!(
            outcome.hit,
            "旧缓存残留运行期条目（含摘要失配）按同律豁免：存量装机免重烘焙即命中"
        );
        assert_eq!(sorted_ids(&manifests).len(), 2);
        assert_eq!(outcome.cache_verified_winner_ids.len(), 2);

        // 同形态下源码漂移仍打穿（豁免不吞源码漂移）
        let tmp2 = tempfile::tempdir().unwrap();
        let plugins2 = write_face(tmp2.path());
        add_runtime_trees(&plugins2);
        write_legacy_cache(&plugins2, |_| {});
        fs::write(plugins2.join("shared/llm/server.py"), b"print('CHANGED')\n").unwrap();
        let loader2 = PluginLoaderImpl::new(plugins2.join("shared"), None::<PathBuf>);
        let roots2 = boot_roots(&plugins2);
        let root_refs2 = as_refs(&roots2);
        let (_, outcome2) = loader2.discover_boot_cached(&root_refs2).await.unwrap();
        assert!(!outcome2.hit, "旧缓存形态下源码漂移必须照常回退全量");
    }

    /// plugin.yaml 兜底 manifest 同样经缓存候选（与 scan_root 同款双解析）。
    #[tokio::test]
    async fn yaml_manifest_served_from_cache() {
        let _env_lock = crate::test_env::user_space_env_lock();
        let tmp = tempfile::tempdir().unwrap();
        let plugins = write_face(tmp.path());
        fs::remove_file(plugins.join("shared/llm/plugin.json")).unwrap();
        fs::write(
            plugins.join("shared/llm/plugin.yaml"),
            "id: llm_service\nname: Test llm\nversion: \"0.1.0\"\nplugin_type: tool\nlanguage: python\nhost_type: sidecar\nentry: python server.py\ncapabilities: {}\n",
        )
        .unwrap();
        // 手写缓存：候选表登记 yaml 文件名，files 表以现盘字节为准
        let mut v: serde_json::Value = serde_json::from_slice(&cache_bytes(&plugins, |v| {
            v["plugin_dirs"][plugins.join("shared").to_string_lossy().as_ref()]["llm"] =
                serde_json::Value::String("plugin.yaml".into());
        }))
        .unwrap();
        // cache_bytes 已含被删 json 的摘要——重建 files 表剔除它
        let face_cut = plugins.to_string_lossy().len() + 1;
        let mut files = serde_json::Map::new();
        let mut stack = vec![plugins.clone()];
        while let Some(dir) = stack.pop() {
            for entry in fs::read_dir(&dir).unwrap().flatten() {
                let p = entry.path();
                if p.is_dir() {
                    stack.push(p);
                } else {
                    let rel = p.to_string_lossy()[face_cut..].replace('\\', "/");
                    let data = fs::read(&p).unwrap();
                    files.insert(rel, serde_json::Value::String(sha256_hex(&data)));
                }
            }
        }
        v["files"] = serde_json::Value::Object(files);
        fs::write(
            plugins.join("shared/discovery-cache.json"),
            (serde_json::to_string(&v).unwrap() + "\n").into_bytes(),
        )
        .unwrap();
        let loader = PluginLoaderImpl::new(plugins.join("shared"), None::<PathBuf>);
        let roots = boot_roots(&plugins);
        let root_refs = as_refs(&roots);
        let (manifests, outcome) = loader.discover_boot_cached(&root_refs).await.unwrap();
        assert!(outcome.hit, "yaml manifest 候选同样短路");
        assert_eq!(sorted_ids(&manifests).len(), 2);
    }

    /// env 覆盖缓存路径（AGENTOS_DISCOVERY_CACHE）与默认位并存时的取位顺序。
    #[test]
    fn cache_path_env_override_wins_over_default() {
        let _guard = crate::test_env::CachePathEnvGuard::set("Z:/elsewhere/cache.json");
        let tmp = tempfile::tempdir().unwrap();
        let loader = PluginLoaderImpl::new(tmp.path(), None::<PathBuf>);
        assert_eq!(
            loader.discovery_cache_path(),
            PathBuf::from("Z:/elsewhere/cache.json")
        );
    }

    /// 符号链接面走查（link 目标漂移判回退；仅 unix CI 可建符号链接——
    /// Windows 无特权建链，本地跳过，链路在 Linux 车道覆盖）。
    #[cfg(unix)]
    #[tokio::test]
    async fn symlink_drift_falls_back() {
        let _env_lock = crate::test_env::user_space_env_lock();
        use std::os::unix::fs::symlink;
        let tmp = tempfile::tempdir().unwrap();
        let plugins = write_face(tmp.path());
        symlink("server.py", plugins.join("shared/llm/link.py")).unwrap();
        let mut v: serde_json::Value =
            serde_json::from_slice(&cache_bytes(&plugins, |_| {})).unwrap();
        v["links"]["shared/llm/link.py"] = serde_json::Value::String("server.py".into());
        fs::write(
            plugins.join("shared/discovery-cache.json"),
            (serde_json::to_string(&v).unwrap() + "\n").into_bytes(),
        )
        .unwrap();
        let loader = PluginLoaderImpl::new(plugins.join("shared"), None::<PathBuf>);
        let roots = boot_roots(&plugins);
        let root_refs = as_refs(&roots);
        // 目标一致 → 命中
        let (_, outcome) = loader.discover_boot_cached(&root_refs).await.unwrap();
        assert!(outcome.hit, "符号链接目标一致必须命中");
        // 目标漂移 → 回退
        let tmp2 = tempfile::tempdir().unwrap();
        let plugins2 = write_face(tmp2.path());
        symlink("other.py", plugins2.join("shared/llm/link.py")).unwrap();
        let mut v2: serde_json::Value = serde_json::from_slice(&cache_bytes(&plugins2, |v| {
            v["links"]["shared/llm/link.py"] = serde_json::Value::String("server.py".into());
        }))
        .unwrap();
        let face_cut = plugins2.to_string_lossy().len() + 1;
        let mut files = serde_json::Map::new();
        let mut stack = vec![plugins2.clone()];
        while let Some(dir) = stack.pop() {
            for entry in fs::read_dir(&dir).unwrap().flatten() {
                let p = entry.path();
                if p.is_dir() {
                    stack.push(p);
                } else if p.is_file() {
                    let rel = p.to_string_lossy()[face_cut..].replace('\\', "/");
                    let data = fs::read(&p).unwrap();
                    files.insert(rel, serde_json::Value::String(sha256_hex(&data)));
                }
            }
        }
        v2["files"] = serde_json::Value::Object(files);
        fs::write(
            plugins2.join("shared/discovery-cache.json"),
            (serde_json::to_string(&v2).unwrap() + "\n").into_bytes(),
        )
        .unwrap();
        let loader2 = PluginLoaderImpl::new(plugins2.join("shared"), None::<PathBuf>);
        let roots2 = boot_roots(&plugins2);
        let root_refs2 = as_refs(&roots2);
        let (_, outcome2) = loader2.discover_boot_cached(&root_refs2).await.unwrap();
        assert!(!outcome2.hit, "符号链接目标漂移必须回退");
    }
}
