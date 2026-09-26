//! Python sidecar venv 自愈（boot 后台）——BUG-55 方向③。
//!
//! 打包 extraResources 明确排除 `.venv`（体积膨胀 + venv 含绝对路径不可重定位），
//! 装机首启的 Python sidecar 全体缺 venv 解释器；spawn 期按 uv 单轨契约
//! fail-closed（无 PATH python 回退，`resolve_sidecar_command` 报
//! VENV_INTERPRETER_MISSING）。dev 的 venv 由 launcher（start_web_02.bat Step 2）
//! 负责；装机链没有 launcher，本模块把同款重建语义搬到内核 boot 后台：
//!
//! - 门控：`AGENTOS_PLUGIN_VENV_AUTOPROVISION=1`（electron `buildKernelEnv` 在
//!   打包链设置；dev 不设，重建权仍在 launcher，避免无 uv 环境的 warn 噪声）；
//! - 选形：已发现插件中 entry 首词为裸 python、有 pyproject.toml、缺 venv
//!   解释器者（与 spawn 期 fail-closed 判据同源，复用 invoker 同一探测函数）；
//!   **合宿成员（host_group 声明）不入选**——它们经 `_host` 共享宿主 spawn，
//!   自身 .venv 运行期零消费（venv 去重 ADR 2026-09-07 的装机侧落法）；
//! - 共享宿主优先：存在合宿成员时先重建 `_host` 共享 venv（其 spawn 期
//!   fail-closed 只认共享 venv，`HOST_VENV_MISSING` 无成员自身回退——2026-09-19
//!   装机日志 101 条该错误即此缺口）；
//! - 重建：`uv sync --project <插件目录>`（与 launcher 同款；uv sync 自带
//!   venv 创建，幂等——已有 .venv 的目录本就不入选）；
//! - 降级：uv 缺席/失败/超时 → 逐插件 warn（行为不劣于现状：spawn 期保持
//!   fail-closed 与修复指引），绝不阻断启动、绝不动任何已有 `.venv`。
//!
//! [来源: ADR 2026-09-20-packaged-dual-source-adjudication 决策②；
//!  去重规则: ADR 2026-09-07-plugin-venv-dedup]
// @feature: FP-0.2.一 插件协议 Python sidecar venv 自愈(BUG-55 方向③) | @ci: rust-test

use std::collections::HashMap;
use std::path::PathBuf;
use std::time::Duration;

use agentos_core::traits::PluginManifest;
use agentos_invoker::{
    builtin_group_host_dir, find_group_host_dir, find_venv_interpreter, is_cohost_member,
    is_plain_python_command,
};
use tracing::{info, warn};

/// venv 自愈门控环境变量（装机链由 electron `buildKernelEnv` 设为 `1`）。
pub const VENV_AUTOPROVISION_ENV: &str = "AGENTOS_PLUGIN_VENV_AUTOPROVISION";

/// 单插件 `uv sync` 上限：冷缓存装 litellm 级依赖可到分钟级，超时按失败降级
/// （warn 留痕，下轮 boot 重试），绝不无限挂住后台任务。
const SYNC_TIMEOUT_SECS: u64 = 300;

/// 并发度：对齐 sidecar 预热（4 路把 ~30 插件压进 ~10s 量级的经验值）。
const CONCURRENCY: usize = 4;

/// 门控是否打开。未知值一律视为关闭（保守：自愈是增强不是默认行为）。
pub fn autoprovision_enabled() -> bool {
    matches!(std::env::var(VENV_AUTOPROVISION_ENV).as_deref(), Ok("1"))
}

/// 选出需要重建 venv 的插件：`(plugin_id, 插件目录)`。
///
/// 判据与 spawn 期 `resolve_sidecar_command` 的 fail-closed 链同源：
/// entry 首词是 PATH 裸 python（[`is_plain_python_command`]）+ 目录有
/// pyproject.toml + venv 解释器缺失（[`find_venv_interpreter`]）。三者任一不
/// 满即跳过——pyproject 缺失者重建也无从谈起（spawn 期的显式报错指引才是正解）。
/// **合宿成员恒不入选**：其 spawn 只认 `_host` 共享 venv，自身 .venv 运行期
/// 零消费，建了纯属装机磁盘浪费（去重 ADR 的装机侧落法）。
pub fn select_missing_venv_plugins(
    manifests: &[PluginManifest],
    plugin_dirs: &HashMap<String, PathBuf>,
) -> Vec<(String, PathBuf)> {
    manifests
        .iter()
        .filter_map(|m| {
            if is_cohost_member(m) {
                return None;
            }
            let first_word = m.entry.split_whitespace().next()?;
            if !is_plain_python_command(first_word) {
                return None;
            }
            let dir = plugin_dirs.get(&m.id)?;
            if !dir.join("pyproject.toml").is_file() {
                return None;
            }
            if find_venv_interpreter(dir).is_some() {
                return None;
            }
            Some((m.id.clone(), dir.clone()))
        })
        .collect()
}

/// 选出需要重建的共享合宿宿主目录（`_host/`）。
///
/// 存在合宿成员时其 spawn 只认 `_host` 共享 venv（fail-closed，无成员自身
/// venv 回退），宿主 venv 是合宿全体可用性的前置，必须优先重建。定位序与
/// spawn 期 `resolve_group_host_command` 同源：从任一成员插件目录向上探测
/// `_host/`，探测全空回退内置根（`AGENTOS_PLUGINS_DIR`）下的 `_host/`。
/// 判据同选形三件套：目录在 + pyproject.toml 在 + venv 解释器缺失。
pub fn select_shared_host_target(
    manifests: &[PluginManifest],
    plugin_dirs: &HashMap<String, PathBuf>,
) -> Option<PathBuf> {
    if !manifests.iter().any(is_cohost_member) {
        return None;
    }
    let host_dir = manifests
        .iter()
        .filter(|m| is_cohost_member(m))
        .filter_map(|m| plugin_dirs.get(&m.id))
        .find_map(|dir| find_group_host_dir(dir))
        .or_else(builtin_group_host_dir)?;
    if !host_dir.join("pyproject.toml").is_file() {
        return None;
    }
    if find_venv_interpreter(&host_dir).is_some() {
        return None;
    }
    Some(host_dir)
}

/// 对单个插件目录跑 `uv sync --project <dir>`（venv 创建 + 按锁文件装依赖，
/// 与 dev launcher 同款语义）。成功返回 Ok；uv 缺席/失败/超时以可读错误 Err。
pub async fn sync_plugin_venv(dir: PathBuf) -> Result<(), String> {
    let command = tokio::process::Command::new("uv")
        .args(["sync", "--project"])
        .arg(&dir)
        .output();
    let output = tokio::time::timeout(Duration::from_secs(SYNC_TIMEOUT_SECS), command)
        .await
        .map_err(|_| format!("uv sync 超时（>{}s）: {}", SYNC_TIMEOUT_SECS, dir.display()))?
        .map_err(|e| format!("uv 进程拉起失败（uv 是否在 PATH？）: {e}"))?;
    if output.status.success() {
        Ok(())
    } else {
        Err(format!(
            "uv sync 失败（exit {:?}）: {}",
            output.status.code(),
            String::from_utf8_lossy(&output.stderr).trim()
        ))
    }
}

/// 一轮自愈的结论（日志消费面）。
pub struct VenvProvisionReport {
    /// 重建成功的插件 id。
    pub synced: Vec<String>,
    /// 重建失败的 `(plugin_id, 错误摘要)`。
    pub failed: Vec<(String, String)>,
}

/// 对目标集执行重建（并发 [`CONCURRENCY`]），返回逐插件结论。
///
/// `runner` 注入执行体（生产传 [`sync_plugin_venv`]，测试传假 runner）——
/// 单插件失败不牵连其余；全部跑完后由调用方记汇总日志。
pub async fn provision_missing_venvs<F, Fut>(
    targets: Vec<(String, PathBuf)>,
    runner: F,
) -> VenvProvisionReport
where
    F: Fn(PathBuf) -> Fut + Clone + Send + 'static,
    Fut: std::future::Future<Output = Result<(), String>> + Send + 'static,
{
    let semaphore = std::sync::Arc::new(tokio::sync::Semaphore::new(CONCURRENCY));
    let mut handles = tokio::task::JoinSet::new();
    for (plugin_id, dir) in targets {
        let semaphore = semaphore.clone();
        let runner = runner.clone();
        handles.spawn(async move {
            // 并发闸：permit 持有至本任务结束
            let _permit = semaphore.acquire().await;
            let result = runner(dir.clone()).await;
            (plugin_id, result)
        });
    }
    let mut report = VenvProvisionReport {
        synced: Vec::new(),
        failed: Vec::new(),
    };
    while let Some(joined) = handles.join_next().await {
        match joined {
            Ok((plugin_id, Ok(()))) => {
                info!(target: "venv-provision", plugin = %plugin_id, "venv 重建完成");
                report.synced.push(plugin_id);
            }
            Ok((plugin_id, Err(error))) => {
                warn!(target: "venv-provision", plugin = %plugin_id, error = %error,
                    "venv 自愈失败（spawn 期保持 fail-closed；下轮 boot 重试）");
                report.failed.push((plugin_id, error));
            }
            Err(e) => warn!(target: "venv-provision", error = %e, "venv 自愈任务崩溃（跳过）"),
        }
    }
    report
}

/// 生产封装：先重建共享合宿宿主 venv（合宿全体 spawn 的前置），再跑独立
/// 插件轮并记汇总（boot 后台 tokio::spawn 的任务体；两者皆可为空 no-op）。
///
/// 宿主重建失败不阻断独立插件轮——但合宿成员将无法 spawn（spawn 期
/// `HOST_VENV_MISSING`），warn 留痕给 boot 日志验收面。
pub async fn provision_and_log(core: Vec<(String, PathBuf)>, host: Option<PathBuf>) {
    provision_and_log_with(core, host, sync_plugin_venv).await;
}

/// 可注入 runner 的生产封装（测试注入假 runner；日志消费面与 [`provision_and_log`] 一致）。
pub async fn provision_and_log_with<F, Fut>(
    core: Vec<(String, PathBuf)>,
    host: Option<PathBuf>,
    runner: F,
) -> VenvProvisionReport
where
    F: Fn(PathBuf) -> Fut + Clone + Send + 'static,
    Fut: std::future::Future<Output = Result<(), String>> + Send + 'static,
{
    if let Some(dir) = host.as_ref() {
        match runner(dir.clone()).await {
            Ok(()) => info!(
                target: "venv-provision",
                dir = %dir.display(),
                "共享合宿宿主 venv 重建完成（host_group=light 成员经它 spawn，自身不再建 venv）"
            ),
            Err(error) => warn!(
                target: "venv-provision",
                dir = %dir.display(), error = %error,
                "共享合宿宿主 venv 重建失败——合宿成员 spawn 将报 HOST_VENV_MISSING（独立插件不受影响）"
            ),
        }
    }
    let report = provision_missing_venvs(core, runner).await;
    info!(
        target: "venv-provision",
        synced = report.synced.len(), failed = report.failed.len(),
        "独立插件 venv 自愈轮完成"
    );
    report
}

#[cfg(test)]
mod tests {
    use super::*;
    use agentos_core::traits::{HostType, PluginType};

    fn manifest_with_entry(id: &str, entry: &str) -> PluginManifest {
        PluginManifest {
            force_include_tools: Vec::new(),
            state: None,
            id: id.to_string(),
            name: id.to_string(),
            description: None,
            version: "1.0.0".to_string(),
            plugin_type: PluginType::System,
            pipeline_role: None,
            language: "python".to_string(),
            host_type: HostType::Sidecar,
            host_group: None,
            entry: entry.to_string(),
            capabilities: Default::default(),
            requires_services: vec![],
            permissions: Default::default(),
            priority: 100,
            mcp: None,
            lifecycle: None,
            native: None,
            granted_capabilities: vec![],
            restricted_capabilities: vec![],
            requires_content: None,
            invoke_entry: None,
            config_files: vec![],
            http_endpoints: vec![],
            ui_schema: None,
            contributes: None,
            enabled: None,
            activation: None,
            provides: None,
            persistent_fields: vec![],
            export_fields: vec![],
        }
    }

    /// 造一个"缺 venv 的 Python sidecar"目录：pyproject.toml 在、无 .venv。
    fn missing_venv_dir(root: &std::path::Path, id: &str) -> PathBuf {
        let dir = root.join(id);
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::write(dir.join("pyproject.toml"), "[project]\nname = \"x\"\n").unwrap();
        dir
    }

    #[test]
    fn selects_plain_python_sidecar_missing_venv() {
        let tmp = tempfile::tempdir().unwrap();
        let dir = missing_venv_dir(tmp.path(), "llm_like");
        let manifests = vec![manifest_with_entry("llm_like", "python server.py")];
        let mut dirs = HashMap::new();
        dirs.insert("llm_like".to_string(), dir);

        let targets = select_missing_venv_plugins(&manifests, &dirs);
        assert_eq!(
            targets.len(),
            1,
            "裸 python entry + pyproject + 缺 venv 必须入选"
        );
        assert_eq!(targets[0].0, "llm_like");
    }

    #[test]
    fn skips_plugin_with_venv_present() {
        let tmp = tempfile::tempdir().unwrap();
        let dir = missing_venv_dir(tmp.path(), "has_venv");
        // 布局探测双平台兜底：造 unix 布局文件即跨平台命中
        std::fs::create_dir_all(dir.join(".venv").join("bin")).unwrap();
        std::fs::write(dir.join(".venv").join("bin").join("python"), "").unwrap();

        let manifests = vec![manifest_with_entry("has_venv", "python server.py")];
        let mut dirs = HashMap::new();
        dirs.insert("has_venv".to_string(), dir);

        assert!(
            select_missing_venv_plugins(&manifests, &dirs).is_empty(),
            "已有 venv 解释器的插件必须跳过（幂等不重建）"
        );
    }

    #[test]
    fn skips_non_python_entry() {
        let tmp = tempfile::tempdir().unwrap();
        let dir = missing_venv_dir(tmp.path(), "node_like");
        let manifests = vec![manifest_with_entry("node_like", "node server.js")];
        let mut dirs = HashMap::new();
        dirs.insert("node_like".to_string(), dir);

        assert!(select_missing_venv_plugins(&manifests, &dirs).is_empty());
    }

    #[test]
    fn skips_missing_pyproject() {
        let tmp = tempfile::tempdir().unwrap();
        let dir = tmp.path().join("no_pyproject");
        std::fs::create_dir_all(&dir).unwrap();
        let manifests = vec![manifest_with_entry("no_pyproject", "python server.py")];
        let mut dirs = HashMap::new();
        dirs.insert("no_pyproject".to_string(), dir);

        assert!(
            select_missing_venv_plugins(&manifests, &dirs).is_empty(),
            "缺 pyproject.toml 不入选（uv sync 无从执行；spawn 期显式报错指引）"
        );
    }

    #[test]
    fn skips_manifest_without_discovered_dir() {
        let manifests = vec![manifest_with_entry("ghost", "python server.py")];
        let dirs = HashMap::new();
        assert!(select_missing_venv_plugins(&manifests, &dirs).is_empty());
    }

    /// 执行面：每个入选目标恰好跑一次 runner；成败分流入 report；单插件失败
    /// 不牵连其余。
    #[tokio::test]
    async fn provision_runs_each_target_once_and_splits_results() {
        let targets = vec![
            ("good_a".to_string(), PathBuf::from("/a")),
            ("good_b".to_string(), PathBuf::from("/b")),
            ("bad".to_string(), PathBuf::from("/c")),
        ];
        let report = provision_missing_venvs(targets, |dir| async move {
            if dir == std::path::Path::new("/c") {
                Err("uv sync 失败（exit Some(1))".to_string())
            } else {
                Ok(())
            }
        })
        .await;

        let mut synced = report.synced;
        synced.sort();
        assert_eq!(synced, vec!["good_a".to_string(), "good_b".to_string()]);
        assert_eq!(report.failed.len(), 1);
        assert_eq!(report.failed[0].0, "bad");
        assert!(report.failed[0].1.contains("uv sync 失败"));
    }

    /// 空目标集是 no-op（不 spawn 任何任务）。
    #[tokio::test]
    async fn provision_empty_targets_is_noop() {
        let report = provision_missing_venvs(Vec::new(), |_| async { Ok(()) }).await;
        assert!(report.synced.is_empty() && report.failed.is_empty());
    }

    fn manifest_with_group(id: &str, entry: &str, host_group: Option<&str>) -> PluginManifest {
        let mut m = manifest_with_entry(id, entry);
        m.host_group = host_group.map(str::to_string);
        m
    }

    #[test]
    fn skips_cohost_light_member_own_venv() {
        // 合宿成员即使缺自身 venv 也不入选：spawn 只认 _host 共享 venv，
        // 成员自身 venv 运行期零消费（装机侧去重的核心断言）。
        let tmp = tempfile::tempdir().unwrap();
        let dir = missing_venv_dir(tmp.path(), "light_like");
        let manifests = vec![manifest_with_group(
            "light_like",
            "python server.py",
            Some("light"),
        )];
        let mut dirs = HashMap::new();
        dirs.insert("light_like".to_string(), dir);

        assert!(
            select_missing_venv_plugins(&manifests, &dirs).is_empty(),
            "合宿成员不得进独立 venv 重建轮"
        );
    }

    /// 布局：<base>/m1（合宿成员）+ <base>/_host（宿主，缺 venv）。
    fn cohost_layout(root: &std::path::Path) -> (PathBuf, PathBuf) {
        let base = root.join("light_group");
        let member = base.join("m1");
        std::fs::create_dir_all(&member).unwrap();
        std::fs::write(member.join("pyproject.toml"), "[project]\nname = \"x\"\n").unwrap();
        let host = base.join("_host");
        std::fs::create_dir_all(&host).unwrap();
        std::fs::write(host.join("pyproject.toml"), "[project]\nname = \"host\"\n").unwrap();
        (member, host)
    }

    fn member_dirs(member: &std::path::Path) -> HashMap<String, PathBuf> {
        let mut dirs = HashMap::new();
        dirs.insert("m1".to_string(), member.to_path_buf());
        dirs
    }

    #[test]
    fn selects_shared_host_target_when_cohost_members_exist() {
        let tmp = tempfile::tempdir().unwrap();
        let (member, host) = cohost_layout(tmp.path());
        let manifests = vec![manifest_with_group("m1", "python server.py", Some("light"))];
        let dirs = member_dirs(&member);

        assert_eq!(
            select_shared_host_target(&manifests, &dirs),
            Some(host),
            "有合宿成员且宿主缺 venv：必须选中共享宿主目录"
        );
        assert!(select_missing_venv_plugins(&manifests, &dirs).is_empty());
    }

    #[test]
    fn shared_host_target_skipped_when_venv_present() {
        let tmp = tempfile::tempdir().unwrap();
        let (member, host) = cohost_layout(tmp.path());
        // 布局探测双平台兜底：造 unix 布局文件即跨平台命中
        std::fs::create_dir_all(host.join(".venv").join("bin")).unwrap();
        std::fs::write(host.join(".venv").join("bin").join("python"), "").unwrap();
        let manifests = vec![manifest_with_group("m1", "python server.py", Some("light"))];

        assert_eq!(
            select_shared_host_target(&manifests, &member_dirs(&member)),
            None,
            "宿主 venv 已在（幂等）不得重建"
        );
    }

    #[test]
    fn shared_host_target_none_without_cohost_members() {
        let tmp = tempfile::tempdir().unwrap();
        let (member, _host) = cohost_layout(tmp.path());
        // 同一布局、成员未声明 host_group：无合宿需求就不动 _host
        let manifests = vec![manifest_with_entry("m1", "python server.py")];

        assert_eq!(
            select_shared_host_target(&manifests, &member_dirs(&member)),
            None
        );
    }

    /// 执行序：共享宿主先于独立轮（宿主是合宿 spawn 的前置）。
    #[tokio::test]
    async fn host_venv_synced_before_core_round() {
        let order = std::sync::Arc::new(std::sync::Mutex::new(Vec::<String>::new()));
        let order2 = order.clone();
        let core = vec![("core_a".to_string(), PathBuf::from("/a"))];
        let report =
            provision_and_log_with(core, Some(PathBuf::from("/host")), move |dir: PathBuf| {
                let order = order2.clone();
                async move {
                    order
                        .lock()
                        .unwrap()
                        .push(dir.to_string_lossy().into_owned());
                    Ok(())
                }
            })
            .await;

        assert_eq!(
            *order.lock().unwrap(),
            vec![
                PathBuf::from("/host").to_string_lossy().into_owned(),
                PathBuf::from("/a").to_string_lossy().into_owned(),
            ],
            "宿主 venv 必须先于独立插件轮重建"
        );
        assert_eq!(report.synced, vec!["core_a".to_string()]);
    }

    /// 唯一走真 uv 的用例（关键路径真实依赖）：空依赖项目 uv sync 秒级完成；
    /// uv 缺席的环境显式跳过（CI/开发机均有 uv）。
    #[tokio::test]
    async fn sync_plugin_venv_real_uv_builds_missing_venv() {
        let uv_available = tokio::process::Command::new("uv")
            .arg("--version")
            .output()
            .await
            .map(|o| o.status.success())
            .unwrap_or(false);
        if !uv_available {
            eprintln!("uv 不在 PATH，跳过真实重建用例");
            return;
        }
        let tmp = tempfile::tempdir().unwrap();
        let dir = tmp.path().join("real_sync");
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::write(
            dir.join("pyproject.toml"),
            "[project]\nname = \"real-sync-probe\"\nversion = \"0.1.0\"\nrequires-python = \">=3.9\"\ndependencies = []\n",
        )
        .unwrap();

        sync_plugin_venv(dir.clone()).await.expect("uv sync 应成功");
        assert!(
            find_venv_interpreter(&dir).is_some(),
            "重建后 venv 解释器必须可被 spawn 期同一探测器命中"
        );
    }
}
