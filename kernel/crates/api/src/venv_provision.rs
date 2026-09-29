//! Python sidecar venv 自愈（boot 后台）——BUG-55 方向③。
//!
//! 打包 extraResources 明确排除 `.venv`（体积膨胀 + venv 含绝对路径不可重定位），
//! 装机首启的 Python sidecar 全体缺 venv 解释器；spawn 期按 uv 单轨契约
//! fail-closed（无 PATH python 回退，`resolve_sidecar_command` 报
//! VENV_INTERPRETER_MISSING，用户空间登记处兜底见 invoker 同名解析序）。dev 的
//! venv 由 launcher（start_web_02.bat Step 2）负责；装机链没有 launcher，本模块
//! 把同款重建语义搬到内核 boot 后台：
//!
//! - 门控：`AGENTOS_PLUGIN_VENV_AUTOPROVISION=1`（electron `buildKernelEnv` 在
//!   打包链设置；dev 不设，重建权仍在 launcher，避免无 uv 环境的 warn 噪声）；
//! - 选形：已发现插件中 entry 首词为裸 python、有 pyproject.toml、缺 venv
//!   解释器者（与 spawn 期 fail-closed 判据同源，复用 invoker 同一探测函数）；
//!   插件目录与用户空间登记处（`<USER_ROOT>/plugin-venvs/<键>`）均缺才算缺；
//!   **合宿成员（host_group 声明）不入选**——它们经 `_host` 共享宿主 spawn，
//!   自身 .venv 运行期零消费（venv 去重 ADR 2026-09-07 的装机侧落法）；
//! - 共享宿主优先：存在合宿成员时先重建 `_host` 共享 venv（其 spawn 期
//!   fail-closed 只认共享 venv）；
//! - **重建落点（ADR 2026-09-28-venv-user-space-provisioning）**：插件目录可写
//!   （纯 dev）→ 原地 `.venv`（launcher 同款，现状不变）；插件目录不可写
//!   （装机态 TrustedInstaller ACL）→ `uv sync --frozen` +
//!   `UV_PROJECT_ENVIRONMENT=<USER_ROOT>/plugin-venvs/<键>` 重定向到用户空间
//!   登记处（`--frozen`：只读工程目录绝无锁文件回写；登记键 = 独占插件 id /
//!   `_host`），**绝不写安装目录**；
//! - 降级：uv 缺席/失败/超时 → 逐插件 warn（行为不劣于现状：spawn 期保持
//!   fail-closed 与修复指引），绝不阻断启动、绝不动任何已有 `.venv`。
//!
//! [来源: ADR 2026-09-20-packaged-dual-source-adjudication 决策②；
//!  去重规则: ADR 2026-09-07-plugin-venv-dedup；
//!  用户空间落点: ADR 2026-09-28-venv-user-space-provisioning]
// @feature: FP-0.2.一 插件协议 Python sidecar venv 自愈(BUG-55 方向③) | @ci: rust-test

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::time::Duration;

use agentos_core::traits::PluginManifest;
use agentos_core::user_space;
// 登记处消费面走 `invoker` 公开模块（lib.rs 根 re-export 保持原样不扩面——
// 无可执行语句的 re-export 头文件不在 llvm-cov 度量面内，见 diff coverage 口径）。
use agentos_invoker::invoker::{find_user_space_venv_interpreter, GROUP_HOST_DIR};
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
            if venv_available(&m.id, dir) {
                return None;
            }
            Some((m.id.clone(), dir.clone()))
        })
        .collect()
}

/// 「venv 可用」判据（与 spawn 期解析序同源）：插件目录 `.venv` 优先，缺席时
/// 用户空间登记处兜底命中同样算可用（autoprovision 不得重建 spawn 期能解析到
/// 的插件——幂等不重装）。
fn venv_available(plugin_id: &str, plugin_dir: &Path) -> bool {
    find_venv_interpreter(plugin_dir).is_some()
        || find_user_space_venv_interpreter(plugin_id).is_some()
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
    if venv_available(GROUP_HOST_DIR, &host_dir) {
        return None;
    }
    Some(host_dir)
}

/// 单个 venv 重建任务：工程目录（`uv sync --project` 的锁文件/清单来源）+
/// venv 落点（`None` = 工程内 `.venv` 原地，dev 现行为；`Some` = 用户空间登记处
/// 绝对路径，装机态重定向）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct VenvSyncTask {
    pub project_dir: PathBuf,
    pub venv_dir: Option<PathBuf>,
}

/// 探测目录可写：建目录 + 建删探针文件。Windows 上目录只读属性不拦创建、
/// 装机只读是 TrustedInstaller ACL——真实建删是两平台唯一可信判据。
pub fn is_dir_writable(dir: &Path) -> bool {
    if std::fs::create_dir_all(dir).is_err() {
        return false;
    }
    let probe = dir.join(format!(".venv-probe-{}", std::process::id()));
    match std::fs::write(&probe, b"") {
        Ok(()) => {
            let _ = std::fs::remove_file(&probe);
            true
        }
        Err(_) => false,
    }
}

/// 重建落点决策（纯函数，可写性与登记处由调用方注入）：插件目录可写 → `None`
/// （原地 `.venv`，纯 dev 现行为零变化）；不可写（装机态 ACL）→ 登记处
/// `<registry>/<key>`（键 = 独占插件 id / `_host`）；用户空间不可用或键非法 →
/// `None` 退回原地尝试（best effort，失败照旧 warn 降级，不劣于现状）。
pub fn provision_venv_target(
    writable: bool,
    registry: Option<&Path>,
    key: &str,
) -> Option<PathBuf> {
    if writable {
        return None;
    }
    let registry = registry?;
    if key.is_empty() || key.contains('/') || key.contains('\\') || key == "." || key == ".." {
        return None;
    }
    Some(registry.join(key))
}

/// 执行单个重建任务。`venv_dir = None`：`uv sync --project <工程>` 原地建
/// `.venv`（dev launcher 同款）；`Some(dir)`：`uv sync --frozen` +
/// `UV_PROJECT_ENVIRONMENT` 把 venv 落用户空间登记处（`--frozen`：只读工程
/// 目录绝无锁文件回写；工程缺 uv.lock 时显式报错——无锁不可重建，fail-closed）。
/// 成功返回 Ok；uv 缺席/失败/超时以可读错误 Err。
pub async fn sync_plugin_venv(task: VenvSyncTask) -> Result<(), String> {
    let mut command = tokio::process::Command::new("uv");
    command.args(["sync", "--project"]).arg(&task.project_dir);
    if let Some(venv_dir) = &task.venv_dir {
        if !task.project_dir.join("uv.lock").is_file() {
            return Err(format!(
                "插件目录 {} 缺 uv.lock 且目录只读——用户空间重建必须按既有锁文件安装（--frozen），\
                 无锁不可重建；请在 dev 侧补锁后重新打包",
                task.project_dir.display()
            ));
        }
        command
            .arg("--frozen")
            .env("UV_PROJECT_ENVIRONMENT", venv_dir);
    }
    let output = tokio::time::timeout(Duration::from_secs(SYNC_TIMEOUT_SECS), command.output())
        .await
        .map_err(|_| {
            format!(
                "uv sync 超时（>{}s）: {}",
                SYNC_TIMEOUT_SECS,
                task.project_dir.display()
            )
        })?
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
    targets: Vec<(String, VenvSyncTask)>,
    runner: F,
) -> VenvProvisionReport
where
    F: Fn(VenvSyncTask) -> Fut + Clone + Send + 'static,
    Fut: std::future::Future<Output = Result<(), String>> + Send + 'static,
{
    let semaphore = std::sync::Arc::new(tokio::sync::Semaphore::new(CONCURRENCY));
    let mut handles = tokio::task::JoinSet::new();
    for (plugin_id, task) in targets {
        let semaphore = semaphore.clone();
        let runner = runner.clone();
        handles.spawn(async move {
            // 并发闸：permit 持有至本任务结束
            let _permit = semaphore.acquire().await;
            let result = runner(task.clone()).await;
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
/// 每个目标在此落重定向决策（[`provision_venv_target`]）：插件目录可写 →
/// 原地 `.venv`（dev 现行为）；不可写 → 用户空间登记处（宿主键
/// [`GROUP_HOST_DIR`]，独占键 = 插件 id），绝不写安装目录。
///
/// 宿主重建失败不阻断独立插件轮——但合宿成员将无法 spawn（spawn 期
/// `HOST_VENV_MISSING`），warn 留痕给 boot 日志验收面。
pub async fn provision_and_log(core: Vec<(String, PathBuf)>, host: Option<PathBuf>) {
    let registry = user_space::user_plugin_venvs_dir();
    let to_task = |key: &str, dir: &Path| VenvSyncTask {
        project_dir: dir.to_path_buf(),
        venv_dir: provision_venv_target(is_dir_writable(dir), registry.as_deref(), key),
    };
    let core_tasks: Vec<(String, VenvSyncTask)> = core
        .iter()
        .map(|(id, dir)| (id.clone(), to_task(id, dir)))
        .collect();
    let host_task = host.as_ref().map(|dir| to_task(GROUP_HOST_DIR, dir));
    provision_and_log_with(core_tasks, host_task, sync_plugin_venv).await;
}

/// 可注入 runner 的生产封装（测试注入假 runner；日志消费面与 [`provision_and_log`] 一致）。
pub async fn provision_and_log_with<F, Fut>(
    core: Vec<(String, VenvSyncTask)>,
    host: Option<VenvSyncTask>,
    runner: F,
) -> VenvProvisionReport
where
    F: Fn(VenvSyncTask) -> Fut + Clone + Send + 'static,
    Fut: std::future::Future<Output = Result<(), String>> + Send + 'static,
{
    if let Some(task) = host.as_ref() {
        match runner(task.clone()).await {
            Ok(()) => info!(
                target: "venv-provision",
                project = %task.project_dir.display(),
                venv = task.venv_dir.as_ref().map(|d| d.display().to_string())
                    .unwrap_or_else(|| "<工程内 .venv>".to_string()),
                "共享合宿宿主 venv 重建完成（host_group 成员经它 spawn，自身不再建 venv）"
            ),
            Err(error) => warn!(
                target: "venv-provision",
                project = %task.project_dir.display(),
                error = %error,
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

// ── 辅助 venv（manifest 声明驱动，ADR 2026-09-28-aux-venv-autoprovision）──
// 依赖互斥插件（hindsight：API 栈 mcp 1.x vs 宿主 SDK mcp 2.x）的第二个
// 解释器环境，供给机制与主 venv 统一：boot autoprovision 同窗口装配、
// 落点同款重定向（插件目录可写原地 / 只读用户空间登记处）。插件侧不再
// 自建二道供给（装机链反复两头落空的根源——插件自愈代码随包滞后）。

/// 辅助 venv 装配任务（`uv venv` + `uv pip install -r requirements`）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AuxVenvTask {
    pub plugin_id: String,
    /// venv 落点（插件目录内原地名或用户空间登记处绝对路径）。
    pub venv_dir: PathBuf,
    /// 依赖清单绝对路径（插件目录内，随包分发）。
    pub requirements: PathBuf,
    pub python: String,
}

/// 声明字段校验：目录/清单名只允许 `[A-Za-z0-9._-]` 且不得为空（防路径穿越；
/// 分隔符 `/` `\` 不在白名单字符集内）。
pub fn is_safe_aux_name(name: &str) -> bool {
    !name.is_empty()
        && name
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || matches!(c, '.' | '_' | '-'))
}

/// 登记处键：`<插件 id>--<目录名剥前导点>`（如 `hindsight_memory_service--venv-hindsight`；
/// 前置校验保证无分隔符，键空间与主 venv 的纯 id 键不冲突）。
pub fn aux_venv_registry_key(plugin_id: &str, dir: &str) -> String {
    format!("{plugin_id}--{}", dir.trim_start_matches('.'))
}

/// venv 目录下的解释器相对路径（Windows `Scripts\python.exe` / Unix `bin/python`）。
fn aux_venv_interpreter(venv_dir: &Path) -> PathBuf {
    if cfg!(windows) {
        venv_dir.join("Scripts").join("python.exe")
    } else {
        venv_dir.join("bin").join("python")
    }
}

/// 选出需要装配的辅助 venv：manifest 声明合法 + 清单文件在 + 解释器缺失
/// （插件目录原地与用户空间登记处双查——与主 venv [`venv_available`] 同口径）。
/// 落点决策同 [`provision_venv_target`]：插件目录可写原地，只读重定向登记处。
pub fn select_missing_aux_venvs(
    manifests: &[PluginManifest],
    plugin_dirs: &HashMap<String, PathBuf>,
) -> Vec<AuxVenvTask> {
    let registry = user_space::user_plugin_venvs_dir();
    let mut tasks = Vec::new();
    for manifest in manifests {
        if manifest.aux_venvs.is_empty() {
            continue;
        }
        let Some(plugin_dir) = plugin_dirs.get(&manifest.id) else {
            continue;
        };
        for decl in &manifest.aux_venvs {
            if !is_safe_aux_name(&decl.dir) || !is_safe_aux_name(&decl.requirements) {
                warn!(
                    target: "venv-provision",
                    plugin = %manifest.id, dir = %decl.dir,
                    requirements = %decl.requirements,
                    "辅助 venv 声明含非法字符（防穿越），跳过装配"
                );
                continue;
            }
            let in_place = plugin_dir.join(&decl.dir);
            let registry_key = aux_venv_registry_key(&manifest.id, &decl.dir);
            let registered = registry.as_deref().map(|base| base.join(&registry_key));
            let present = aux_venv_interpreter(&in_place).is_file()
                || registered
                    .as_ref()
                    .is_some_and(|dir| aux_venv_interpreter(dir).is_file());
            if present {
                continue;
            }
            let requirements_path = plugin_dir.join(&decl.requirements);
            if !requirements_path.is_file() {
                warn!(
                    target: "venv-provision",
                    plugin = %manifest.id, requirements = %decl.requirements,
                    "辅助 venv 依赖清单缺失（应随包分发），跳过装配"
                );
                continue;
            }
            let venv_dir = provision_venv_target(
                is_dir_writable(plugin_dir),
                registry.as_deref(),
                &registry_key,
            )
            .unwrap_or(in_place);
            tasks.push(AuxVenvTask {
                plugin_id: manifest.id.clone(),
                venv_dir,
                requirements: requirements_path,
                python: decl.python.clone(),
            });
        }
    }
    tasks
}

/// 装配单个辅助 venv：`uv venv <dir> --python <py>` → `uv pip install
/// --python <解释器> -r <清单>`（两步；超时/失败语义同 [`sync_plugin_venv`]）。
pub async fn sync_aux_venv(task: AuxVenvTask) -> Result<(), String> {
    let venv_cmd = {
        let mut c = tokio::process::Command::new("uv");
        c.arg("venv")
            .arg(&task.venv_dir)
            .arg("--python")
            .arg(&task.python);
        c
    };
    let install_cmd = {
        let mut c = tokio::process::Command::new("uv");
        c.arg("pip")
            .arg("install")
            .arg("--python")
            .arg(aux_venv_interpreter(&task.venv_dir))
            .arg("-r")
            .arg(&task.requirements);
        c
    };
    for (mut command, label) in [(venv_cmd, "uv venv"), (install_cmd, "uv pip install")] {
        let output = tokio::time::timeout(Duration::from_secs(SYNC_TIMEOUT_SECS), command.output())
            .await
            .map_err(|_| {
                format!(
                    "{label} 超时（>{SYNC_TIMEOUT_SECS}s）: {}",
                    task.venv_dir.display()
                )
            })?
            .map_err(|e| format!("{label} 进程拉起失败（uv 是否在 PATH？）: {e}"))?;
        if !output.status.success() {
            return Err(format!(
                "{label} 失败（exit {:?}）: {}",
                output.status.code(),
                String::from_utf8_lossy(&output.stderr).trim()
            ));
        }
    }
    Ok(())
}

/// 辅助 venv 装配轮（boot 后台独立于主轮；逐任务 warn 降级不阻断）。
pub async fn provision_aux_and_log(tasks: Vec<AuxVenvTask>) {
    let semaphore = std::sync::Arc::new(tokio::sync::Semaphore::new(CONCURRENCY));
    let mut handles = tokio::task::JoinSet::new();
    for task in tasks {
        let semaphore = semaphore.clone();
        handles.spawn(async move {
            let _permit = semaphore.acquire().await;
            let label = format!("{} ({})", task.plugin_id, task.venv_dir.display());
            (label, sync_aux_venv(task).await)
        });
    }
    let mut synced = 0usize;
    let mut failed = 0usize;
    while let Some(joined) = handles.join_next().await {
        match joined {
            Ok((label, Ok(()))) => {
                synced += 1;
                info!(target: "venv-provision", target = %label, "辅助 venv 装配完成");
            }
            Ok((label, Err(error))) => {
                failed += 1;
                warn!(
                    target: "venv-provision", target = %label, error = %error,
                    "辅助 venv 装配失败（插件侧降级语义承载；下轮 boot 重试）"
                );
            }
            Err(e) => {
                failed += 1;
                warn!(target: "venv-provision", error = %e, "辅助 venv 装配任务崩溃（跳过）")
            }
        }
    }
    info!(
        target: "venv-provision", synced, failed,
        "辅助 venv 装配轮完成（manifest aux_venvs 声明驱动）"
    );
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
            aux_venvs: Vec::new(),
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
        let (_guard, _user) = UserRootGuard::isolate();
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

    /// 用户空间 venv 登记处的环境变量是进程全局态：测试串行并配对清场
    /// （锁守卫作为字段持有——Drop 先恢复 env 再随字段释放锁，恢复不落在
    /// 串行窗之外）。
    static USER_ROOT_LOCK: std::sync::Mutex<()> = std::sync::Mutex::new(());

    struct UserRootGuard {
        original: Option<String>,
        _serial: std::sync::MutexGuard<'static, ()>,
    }

    impl UserRootGuard {
        fn pin_to(path: &std::path::Path) -> Self {
            let _serial = USER_ROOT_LOCK.lock().unwrap_or_else(|e| e.into_inner());
            let original = std::env::var("AGENTOS_USER_ROOT").ok();
            std::env::set_var("AGENTOS_USER_ROOT", path);
            Self { original, _serial }
        }

        /// 钉到空用户根：登记处缺席判定与机器环境残留（%APPDATA%\agentos）
        /// 无关的确定性，且与 pin 用例互斥（选形读登记处的一切用例通用）。
        fn isolate() -> (Self, tempfile::TempDir) {
            let dir = tempfile::tempdir().unwrap();
            (Self::pin_to(dir.path()), dir)
        }
    }

    impl Drop for UserRootGuard {
        fn drop(&mut self) {
            match &self.original {
                Some(v) => std::env::set_var("AGENTOS_USER_ROOT", v),
                None => std::env::remove_var("AGENTOS_USER_ROOT"),
            }
        }
    }

    /// 选形幂等（装机态重装轮）：插件目录 .venv 缺席但用户空间登记处已有
    /// 解释器（上轮重定向重建的产物）→ 不得再入选——spawn 期解析序②能命中
    /// 的插件重建纯属浪费（venv_available 与 spawn 期同源）。
    #[test]
    fn skips_plugin_with_user_space_registered_venv() {
        let tmp = tempfile::tempdir().unwrap();
        let _guard = UserRootGuard::pin_to(tmp.path());
        let plugin_root = missing_venv_dir(&tmp.path().join("plugins"), "llm_like");
        // 登记处布局 = venv 根（Scripts|bin/python），双平台布局同建无死臂
        let registered_dir = tmp.path().join("plugin-venvs").join("llm_like");
        for interp in [
            registered_dir.join("Scripts").join("python.exe"),
            registered_dir.join("bin").join("python"),
        ] {
            std::fs::create_dir_all(interp.parent().unwrap()).unwrap();
            std::fs::write(&interp, b"").unwrap();
        }

        let manifests = vec![manifest_with_entry("llm_like", "python server.py")];
        let mut dirs = HashMap::new();
        dirs.insert("llm_like".to_string(), plugin_root);

        assert!(
            select_missing_venv_plugins(&manifests, &dirs).is_empty(),
            "登记处已有解释器的插件不得再入选（幂等不重装）"
        );
    }

    #[test]
    fn skips_plugin_with_venv_present() {
        let (_guard, _user) = UserRootGuard::isolate();
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
            (
                "good_a".to_string(),
                VenvSyncTask {
                    project_dir: PathBuf::from("/a"),
                    venv_dir: None,
                },
            ),
            (
                "good_b".to_string(),
                VenvSyncTask {
                    project_dir: PathBuf::from("/b"),
                    venv_dir: None,
                },
            ),
            (
                "bad".to_string(),
                VenvSyncTask {
                    project_dir: PathBuf::from("/c"),
                    venv_dir: None,
                },
            ),
        ];
        let report = provision_missing_venvs(targets, |task| async move {
            if task.project_dir == std::path::Path::new("/c") {
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
        let report = provision_missing_venvs(Vec::new(), |_: VenvSyncTask| async { Ok(()) }).await;
        assert!(report.synced.is_empty() && report.failed.is_empty());
    }

    /// 重定向决策四象限（ADR 2026-09-28 解析序的 autoprovision 侧）：
    /// 目录可写 → None（dev 现行为，原地 .venv）；目录不可写 + 登记处可用 →
    /// Some(登记处/键)（装机态，绝不落安装目录）；目录不可写 + 用户空间不可用 →
    /// None（best effort 原地，不劣于现状）；非法键（穿越形态）→ None。
    #[test]
    fn provision_venv_target_quadrants() {
        let registry = Path::new("/user-root/plugin-venvs");

        assert_eq!(
            provision_venv_target(true, Some(registry), "bash_tool"),
            None,
            "目录可写（纯 dev）必须原地重建，现状零变化"
        );
        assert_eq!(
            provision_venv_target(false, Some(registry), "bash_tool"),
            Some(registry.join("bash_tool")),
            "目录不可写（装机态 ACL）必须重定向用户空间登记处"
        );
        assert_eq!(
            provision_venv_target(false, None, "bash_tool"),
            None,
            "用户空间不可用退回原地尝试（失败照旧 warn 降级）"
        );
        for bad_key in ["../evil", "a/b", "a\\b", ".", "..", ""] {
            assert_eq!(
                provision_venv_target(false, Some(registry), bad_key),
                None,
                "非法登记键 {bad_key} 不得拼路径（防注入）"
            );
        }
    }

    /// 目录可写探测：真实临时目录可写为真；以文件为父的伪目录（create_dir_all
    /// 必败）为假——两平台同一代码路径的真实 IO 判据。
    #[test]
    fn is_dir_writable_probes_real_io() {
        let tmp = tempfile::tempdir().unwrap();
        assert!(is_dir_writable(tmp.path()), "真实可写目录必须判真");
        let blocker = tmp.path().join("blocker");
        std::fs::write(&blocker, b"").unwrap();
        assert!(
            !is_dir_writable(&blocker.join("sub")),
            "建目录失败的路径必须判不可写"
        );
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
        let (_guard, _user) = UserRootGuard::isolate();
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
        let (_guard, _user) = UserRootGuard::isolate();
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
        let core = vec![(
            "core_a".to_string(),
            VenvSyncTask {
                project_dir: PathBuf::from("/a"),
                venv_dir: None,
            },
        )];
        let host = VenvSyncTask {
            project_dir: PathBuf::from("/host"),
            venv_dir: None,
        };
        let host_repr = host.project_dir.to_string_lossy().into_owned();
        let core_repr = "/a".to_string();
        let report = provision_and_log_with(core, Some(host), move |task: VenvSyncTask| {
            let order = order2.clone();
            async move {
                order
                    .lock()
                    .unwrap()
                    .push(task.project_dir.to_string_lossy().into_owned());
                Ok(())
            }
        })
        .await;

        assert_eq!(
            *order.lock().unwrap(),
            vec![host_repr, core_repr],
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

        sync_plugin_venv(VenvSyncTask {
            project_dir: dir.clone(),
            venv_dir: None,
        })
        .await
        .expect("uv sync 应成功");
        assert!(
            find_venv_interpreter(&dir).is_some(),
            "重建后 venv 解释器必须可被 spawn 期同一探测器命中"
        );
    }

    /// 重定向真跑（装机态路径的真实依赖）：venv 落点 = 用户空间登记处
    /// （UV_PROJECT_ENVIRONMENT 重定向 + --frozen），工程目录内**绝不出现
    /// .venv**——「不碰插件目录」的性质断言；登记处解释器可被 spawn 期
    /// 同一探测器（find_registered_venv_interpreter 经登记键）命中。
    #[tokio::test]
    async fn sync_plugin_venv_real_uv_redirects_to_user_registry() {
        let uv_available = tokio::process::Command::new("uv")
            .arg("--version")
            .output()
            .await
            .map(|o| o.status.success())
            .unwrap_or(false);
        if !uv_available {
            eprintln!("uv 不在 PATH，跳过真实重定向用例");
            return;
        }
        let tmp = tempfile::tempdir().unwrap();
        let project = tmp.path().join("readonly_like");
        std::fs::create_dir_all(&project).unwrap();
        std::fs::write(
            project.join("pyproject.toml"),
            "[project]\nname = \"redirect-probe\"\nversion = \"0.1.0\"\n\
             requires-python = \">=3.9\"\ndependencies = []\n",
        )
        .unwrap();
        // --frozen 需要既有锁文件（装机树 89/90 插件自带 uv.lock 同前提）
        let lock = tokio::process::Command::new("uv")
            .args(["lock", "--project"])
            .arg(&project)
            .output()
            .await
            .expect("uv lock 拉起");
        assert!(lock.status.success(), "uv lock 应成功");

        let venv_dir = tmp.path().join("registry").join("redirect_probe");
        sync_plugin_venv(VenvSyncTask {
            project_dir: project.clone(),
            venv_dir: Some(venv_dir.clone()),
        })
        .await
        .expect("重定向 uv sync 应成功");

        assert!(
            !project.join(".venv").exists(),
            "重定向重建绝不在工程目录（装机态=只读安装目录）落 .venv"
        );
        let agentos_invoker_probe =
            agentos_invoker::invoker::find_registered_venv_interpreter(&venv_dir);
        assert!(
            agentos_invoker_probe.is_some(),
            "登记处解释器必须可被 spawn 期同一探测器命中"
        );
    }

    /// 重定向模式缺 uv.lock → fail-closed 拒绝（无锁不可重建，绝不碰工程目录）。
    #[tokio::test]
    async fn sync_plugin_venv_redirect_without_lock_fails_closed() {
        let tmp = tempfile::tempdir().unwrap();
        let project = tmp.path().join("no_lock");
        std::fs::create_dir_all(&project).unwrap();
        std::fs::write(project.join("pyproject.toml"), "[project]\n").unwrap();

        let err = sync_plugin_venv(VenvSyncTask {
            project_dir: project.clone(),
            venv_dir: Some(tmp.path().join("registry").join("no_lock")),
        })
        .await
        .expect_err("缺 uv.lock 的重定向重建必须失败");
        assert!(err.contains("uv.lock"), "错误须指明锁文件缺口，实际: {err}");
    }

    /// 生产封装端到端（真实依赖，boot-only 入口的覆盖面）：可写工程目录 →
    /// 装配决策落原地（venv_dir=None），uv 真重建后插件目录解释器可探测。
    /// 登记 host 传 None（装机态合宿缺席合法形态）。
    #[tokio::test]
    async fn provision_and_log_writable_project_provisions_in_place() {
        let uv_available = tokio::process::Command::new("uv")
            .arg("--version")
            .output()
            .await
            .map(|o| o.status.success())
            .unwrap_or(false);
        if !uv_available {
            eprintln!("uv 不在 PATH，跳过生产封装用例");
            return;
        }
        let (_guard, _user) = UserRootGuard::isolate();
        let tmp = tempfile::tempdir().unwrap();
        let dir = tmp.path().join("wrap_probe");
        std::fs::create_dir_all(&dir).unwrap();
        // 真 uv sync 需完整 pyproject（requires-python 等；选形用例的桩内容不够）
        std::fs::write(
            dir.join("pyproject.toml"),
            "[project]\nname = \"wrap-probe\"\nversion = \"0.1.0\"\n\
             requires-python = \">=3.9\"\ndependencies = []\n",
        )
        .unwrap();

        provision_and_log(vec![("wrap_probe".to_string(), dir.clone())], None).await;
        assert!(
            find_venv_interpreter(&dir).is_some(),
            "可写目录（dev 现行为）必须经生产封装原地重建 .venv"
        );
    }

    // ── 辅助 venv（manifest 声明驱动，ADR 2026-09-28-aux-venv-autoprovision）──

    use agentos_core::traits::AuxVenvDecl;

    fn aux_manifest(id: &str) -> PluginManifest {
        let mut m = manifest_with_entry(id, "python server.py");
        m.aux_venvs = vec![AuxVenvDecl {
            dir: ".venv-hindsight".to_string(),
            requirements: "requirements.txt".to_string(),
            python: "3.12".to_string(),
        }];
        m
    }

    fn aux_dir(root: &std::path::Path, id: &str) -> PathBuf {
        let dir = root.join(id);
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::write(
            dir.join("requirements.txt"),
            "hindsight-api
",
        )
        .unwrap();
        dir
    }

    #[test]
    fn aux_registry_key_and_safe_name() {
        assert_eq!(
            aux_venv_registry_key("hindsight_memory_service", ".venv-hindsight"),
            "hindsight_memory_service--venv-hindsight"
        );
        assert!(is_safe_aux_name(".venv-hindsight"));
        assert!(is_safe_aux_name("requirements.txt"));
        assert!(!is_safe_aux_name("../escape"));
        assert!(!is_safe_aux_name("a/b"));
        assert!(!is_safe_aux_name(""));
    }

    #[test]
    fn selects_missing_aux_venv_in_place() {
        let (_guard, _user) = UserRootGuard::isolate();
        let tmp = tempfile::tempdir().unwrap();
        let dir = aux_dir(tmp.path(), "hindsight_memory_service");
        let manifests = vec![aux_manifest("hindsight_memory_service")];
        let mut dirs = HashMap::new();
        dirs.insert("hindsight_memory_service".to_string(), dir.clone());

        let tasks = select_missing_aux_venvs(&manifests, &dirs);

        assert_eq!(tasks.len(), 1, "声明合法+清单在+venv 缺 → 入选");
        assert_eq!(tasks[0].plugin_id, "hindsight_memory_service");
        assert_eq!(
            tasks[0].venv_dir,
            dir.join(".venv-hindsight"),
            "可写目录原地装配"
        );
        assert_eq!(tasks[0].requirements, dir.join("requirements.txt"));
    }

    #[test]
    fn skips_when_in_place_venv_present() {
        let (_guard, _user) = UserRootGuard::isolate();
        let tmp = tempfile::tempdir().unwrap();
        let dir = aux_dir(tmp.path(), "hindsight_memory_service");
        let interp = if cfg!(windows) {
            dir.join(".venv-hindsight")
                .join("Scripts")
                .join("python.exe")
        } else {
            dir.join(".venv-hindsight").join("bin").join("python")
        };
        std::fs::create_dir_all(interp.parent().unwrap()).unwrap();
        std::fs::write(&interp, b"").unwrap();
        let manifests = vec![aux_manifest("hindsight_memory_service")];
        let mut dirs = HashMap::new();
        dirs.insert("hindsight_memory_service".to_string(), dir);

        assert!(select_missing_aux_venvs(&manifests, &dirs).is_empty());
    }

    #[test]
    fn skips_when_registered_in_user_space() {
        let user_root = tempfile::tempdir().unwrap();
        let _guard = UserRootGuard::pin_to(user_root.path());
        let tmp = tempfile::tempdir().unwrap();
        let dir = aux_dir(tmp.path(), "hindsight_memory_service");
        let key_dir = user_root
            .path()
            .join("plugin-venvs")
            .join("hindsight_memory_service--venv-hindsight");
        let interp = if cfg!(windows) {
            key_dir.join("Scripts").join("python.exe")
        } else {
            key_dir.join("bin").join("python")
        };
        std::fs::create_dir_all(interp.parent().unwrap()).unwrap();
        std::fs::write(&interp, b"").unwrap();
        let manifests = vec![aux_manifest("hindsight_memory_service")];
        let mut dirs = HashMap::new();
        dirs.insert("hindsight_memory_service".to_string(), dir);

        assert!(
            select_missing_aux_venvs(&manifests, &dirs).is_empty(),
            "用户空间登记处已有解释器 → 不重复装配"
        );
    }

    #[test]
    fn skips_unsafe_or_manifestless_declarations() {
        let (_guard, _user) = UserRootGuard::isolate();
        let tmp = tempfile::tempdir().unwrap();
        let dir = aux_dir(tmp.path(), "hindsight_memory_service");
        let mut bad_path = aux_manifest("hindsight_memory_service");
        bad_path.aux_venvs[0].dir = "../escape".to_string();
        let mut no_reqs = aux_manifest("hindsight_memory_service");
        no_reqs.aux_venvs[0].requirements = "missing.txt".to_string();
        let mut dirs = HashMap::new();
        dirs.insert("hindsight_memory_service".to_string(), dir);

        assert!(
            select_missing_aux_venvs(&[bad_path], &dirs).is_empty(),
            "路径穿越形态拒绝"
        );
        assert!(
            select_missing_aux_venvs(&[no_reqs], &dirs).is_empty(),
            "依赖清单缺失跳过（应随包分发）"
        );
    }
}
