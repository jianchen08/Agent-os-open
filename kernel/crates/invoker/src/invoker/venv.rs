//! venv 解释器解析纯函数族：插件目录/用户空间登记处两形态探测、裸 python
//! 判定与 venv 解释器 spawn 形态守门（恒等透传，防翻回说明见
//! [`resolve_venv_direct_interpreter`]）。
//!
//! 自 invoker.rs 按关注点纯搬运拆出（2026-10-12 文件长度机械闸首个应用案例，
//! ADR docs/decisions/2026-10-12-file-length-mechanical-gate.md）。

use std::path::Path;

/// venv 解释器路径（**Rust 侧布局单源**，纯路径选择逻辑，`windows` 参数使
/// 两平台分支皆可测）。
///
/// `venv_root` 是**直接含 Scripts|bin 的 venv 根**；插件目录形态（`.venv` 子目录）
/// 与用户空间登记处形态（登记项本身即 venv 根）共用本布局。跨 crate 消费：
/// `api::venv_provision` 辅助 venv 装配改引本函数（多端平台差异普查 ⚠️-1
/// 收敛——api/invoker 两 crate 不再各写一份 `Scripts\python.exe` vs
/// `bin/python`），改动布局先过本单点。
pub fn interpreter_layout(venv_root: &Path, windows: bool) -> std::path::PathBuf {
    if windows {
        venv_root.join("Scripts").join("python.exe")
    } else {
        venv_root.join("bin").join("python")
    }
}

/// 插件目录形态：uv venv 标准布局 `.venv/Scripts/python.exe`（Windows）、
/// `.venv/bin/python`（Unix）。
pub(super) fn venv_interpreter_layout(plugin_dir: &Path, windows: bool) -> std::path::PathBuf {
    interpreter_layout(&plugin_dir.join(".venv"), windows)
}

/// 探测插件目录的 venv 解释器：按本平台布局优先，另一平台布局作回退
/// （探测无害——命中即返回绝对路径，缺失返回 None，不执行解释器）。
/// pub：boot 期 venv 自愈（api::venv_provision）与 spawn 期解析共用同一探测。
pub fn find_venv_interpreter(plugin_dir: &Path) -> Option<std::path::PathBuf> {
    [
        venv_interpreter_layout(plugin_dir, cfg!(windows)),
        venv_interpreter_layout(plugin_dir, !cfg!(windows)),
    ]
    .into_iter()
    .find(|p| p.is_file())
}

/// 探测用户空间 venv 登记处的一个登记项（登记项本身即 venv 根，无 `.venv`
/// 中间层——`UV_PROJECT_ENVIRONMENT` 重定向的原生落点形态）。布局探测序与
/// [`find_venv_interpreter`] 同源。
pub fn find_registered_venv_interpreter(venv_dir: &Path) -> Option<std::path::PathBuf> {
    [
        interpreter_layout(venv_dir, cfg!(windows)),
        interpreter_layout(venv_dir, !cfg!(windows)),
    ]
    .into_iter()
    .find(|p| p.is_file())
}

/// venv 登记键合法性：单路径段（无分隔符、非点段）。独占插件键 = 插件 id
/// （G2 校验保证安全段），共享合宿宿主键 = [`GROUP_HOST_DIR`]；防御式拒绝
/// 含穿越形态的键——登记处位于用户空间，键拼路径前必须先挡路径注入。
fn is_valid_venv_key(key: &str) -> bool {
    !key.is_empty() && key != "." && key != ".." && !key.contains('/') && !key.contains('\\')
}

/// spawn 期 venv 解析第 2 优先级：用户空间登记处
/// （`<USER_ROOT>/plugin-venvs/<key>`，[`agentos_core::user_space::user_plugin_venvs_dir`]）。
///
/// 插件目录 `.venv` 恒优先——纯 dev 路径零变化；登记处仅在插件目录解释器
/// 缺席时兜底（装机态插件目录只读、`.venv` 被打包排除，ADR
/// 2026-09-28-venv-user-space-provisioning）。用户空间不可用或键非法 → None
/// （调用方沿 fail-closed 报错）。
pub fn find_user_space_venv_interpreter(key: &str) -> Option<std::path::PathBuf> {
    if !is_valid_venv_key(key) {
        return None;
    }
    let venv_dir = agentos_core::user_space::user_plugin_venvs_dir()?.join(key);
    find_registered_venv_interpreter(&venv_dir)
}

/// 判定 entry 首词是否 PATH 裸 python 命令（`python` / `python3`，含 Windows
/// 可执行扩展 `.exe`/`.bat`/`.cmd`）。带路径分隔符的绝对/相对路径不判——venv
/// 只替代"靠 PATH 解析的裸解释器"，显式绝对路径 entry 是刻意选择，不动。
pub fn is_plain_python_command(command: &str) -> bool {
    if command.contains('\\') || command.contains('/') {
        return false;
    }
    let lower = command.to_ascii_lowercase();
    let name = lower
        .strip_suffix(".exe")
        .or_else(|| lower.strip_suffix(".bat"))
        .or_else(|| lower.strip_suffix(".cmd"))
        .unwrap_or(&lower);
    matches!(name, "python" | "python3")
}

/// venv 解释器解析：**恒等透传**（command/args 原样返回、零 env 注入）。
///
/// 现状契约：venv 解释器以探测到的形态 spawn。Windows venv 的
/// `Scripts/python.exe` 是 trampoline 跳板（spawn base 解释器后自身常驻中转
/// stdio），但它轻量且是**唯一能给出正确 venv 身份**（sys.prefix/sys.executable/
/// site-packages 含 .pth/editable）的入口；Unix venv python 是 symlink 单进程，
/// 同理原样。`windows` 参数保留（调用方平台门与两平台可测性不变），两平台
/// 行为统一恒等。
///
/// 防翻回（2026-10-20 缺陷闭环）：本函数曾按 pyvenv.cfg `home` 把跳板替换为
/// base 真身并注入 `__PYVENV_LAUNCHER__` 锚定——该变量是 **macOS 专属机制**
/// （CPython getpath 仅在 Apple 平台以其值定位 venv），Windows CPython 不识别，
/// 直跑真身 sys.prefix 落回基座 → venv site-packages 全丢 → sidecar 缺真依赖
/// （litellm 半残 import 报 `module 'litellm' has no attribute 'Router'`）→
/// llm.complete_stream 全炸 → 用户桌面端发消息一律「管道异常退出」。Windows
/// CPython 无等价的 venv 身份锚定环境变量，**任何「换真身/消跳板」提案必须
/// 先解决 sys.prefix 锚定**，否则禁翻。跳板带来的进程树观测诉求（空壳 ~5MB
/// vs 真身数百 MB）已由 metrics 观测侧下探解决（api crate `metrics::
/// proc_state` 沿跳板 pid 下探真实工作进程），spawn 层无消层诉求。
pub(super) fn resolve_venv_direct_interpreter(
    command: &str,
    args: Vec<String>,
    _windows: bool,
) -> (String, Vec<String>, Option<(String, String)>) {
    (command.to_string(), args, None)
}

/// spawn 期包装：与 [`resolve_venv_direct_interpreter`] 同一恒等透传契约
/// （`windows` 参数取本平台 `cfg!(windows)`，签名与调用方保持不变）。
pub(super) fn resolve_venv_direct_interpreter_for_spawn(
    command: &str,
    args: Vec<String>,
) -> (String, Vec<String>, Option<(String, String)>) {
    resolve_venv_direct_interpreter(command, args, cfg!(windows))
}

#[cfg(test)]
mod tests {
    use super::*;

    /// 串行锁：env 是进程全局态，本模块用例互斥执行。
    fn tests_guard() -> std::sync::MutexGuard<'static, ()> {
        static LOCK: std::sync::Mutex<()> = std::sync::Mutex::new(());
        LOCK.lock().unwrap_or_else(|e| e.into_inner())
    }

    /// 登记处解析端到端：`AGENTOS_PLUGIN_VENVS_DIR` 覆盖后，spawn 期按 manifest id
    /// 键在 env 指向的登记处取解释器（移动端种子预烤布局的消费面）；键非法仍
    /// fail-closed。
    #[test]
    fn user_space_venv_lookup_honors_dir_override() {
        let _lock = tests_guard();
        let tmp = tempfile::tempdir().unwrap();
        let baked = tmp.path().join("baked-venvs");
        let key_dir = baked.join("bash_tool");
        std::fs::create_dir_all(key_dir.join("bin")).unwrap();
        std::fs::write(key_dir.join("bin").join("python"), b"#!venv").unwrap();

        let original = std::env::var(agentos_core::user_space::PLUGIN_VENVS_DIR_ENV).ok();
        std::env::set_var(agentos_core::user_space::PLUGIN_VENVS_DIR_ENV, &baked);
        let found = find_user_space_venv_interpreter("bash_tool");
        match original {
            Some(v) => std::env::set_var(agentos_core::user_space::PLUGIN_VENVS_DIR_ENV, v),
            None => std::env::remove_var(agentos_core::user_space::PLUGIN_VENVS_DIR_ENV),
        }

        assert_eq!(
            found.unwrap(),
            key_dir.join("bin").join("python"),
            "env 覆盖决定登记处根，键 = manifest id"
        );
        assert_eq!(
            find_user_space_venv_interpreter("../escape"),
            None,
            "登记键合法性不随登记处根变化（路径注入恒拒）"
        );
    }
}
