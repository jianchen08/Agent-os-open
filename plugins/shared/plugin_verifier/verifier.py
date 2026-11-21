"""插件验证执行核（plugin_verify 工具的实现面）。

定位：给**没有宿主 bash**的编码/进化 agent 一个受约束的插件验证入口——
它只能指定"工作空间内哪个插件目录、验后端还是前端"，不能指定解释器、
脚本、环境变量或任意命令。真正的验证动作由安装位内的固定脚本
（`skills/plugin-dev/scripts/verify_host.py`）完成：

- 脚本位置 = 本插件安装根（`plugins/shared/plugin_verifier` 上溯三级），
  不在 agent 可写的工作空间内；脚本 SHA-256 与 `script_pins.json` 不符即
  拒绝执行（脚本锁定，改动须随插件一起提审）。
- 子进程环境白名单化：只透传 PATH/TEMP/系统目录等运行必需键，`AGENTOS_*`
  与云厂商凭证一律不带入。
- 超时杀**进程树**（scratch 内核与其 sidecar 一并清），不留下孤儿内核。
- 证据回传前脱敏（scratch admin 口令恒为 `***`），scratch 目录（含
  instance.yaml 明文口令）在 finally 中删除。

能力边界（如实声明）：验证会把被测插件装载进 scratch 内核，即被测插件
代码会在宿主执行（R2）——这是"真实装载验证"的固有语义，不是本工具
新增的权限面；工具约束的是"用什么脚本、在哪个环境、对哪个目录"执行。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_PLUGIN_ROOT = Path(__file__).resolve().parent
# 仓内/装机包根：<root>/plugins/shared/plugin_verifier/verifier.py → parents[3]。
_APP_ROOT = _PLUGIN_ROOT.parents[2]

_VERIFY_HOST_REL = Path("skills") / "plugin-dev" / "scripts" / "verify_host.py"
_PINNED_SCRIPTS = (
    "skills/plugin-dev/scripts/verify_host.py",
    "skills/plugin-dev/scripts/verify_plugin.py",
    "skills/plugin-dev/scripts/frontend_probe.cjs",
    # verify_plugin 运行时经 importlib 加载的 scratch 实例管理脚本（传递依赖，
    # 漏 pin 则改它可绕过脚本锁）。
    "skills/skill-dev-toolkit/scripts/spawn_test_kernel.py",
)
_PIN_FILE = _PLUGIN_ROOT / "script_pins.json"

_PLUGIN_ID_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_EVIDENCE_NAME = "verify_host_evidence.json"

# 环境白名单：运行固定脚本/Node/Playwright/内核所需的最小集合。
# 刻意不含 AGENTOS_*（scratch 实例的入口配置由脚本自己注入）与任何
# API key / token 类变量（凭证不出境到验证进程）。
_ENV_ALLOWED = (
    "PATH",
    "PATHEXT",
    "COMSPEC",
    "SYSTEMROOT",
    "WINDIR",
    "SYSTEMDRIVE",
    "TEMP",
    "TMP",
    "HOME",
    "USERPROFILE",
    "APPDATA",
    "LOCALAPPDATA",
    "PROGRAMDATA",
    "NUMBER_OF_PROCESSORS",
    "PROCESSOR_ARCHITECTURE",
    "OS",
)
_ENV_FORCED = {"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}

_SCOPES = frozenset({"backend", "frontend"})
_DEFAULT_TIMEOUT_S = 240
_MIN_TIMEOUT_S = 60
_MAX_TIMEOUT_S = 600

# 证据/stdout 脱敏正则：json 形态与 `key: value` 文本形态的口令字段都盖住。
_SECRET_RE = re.compile(r"(admin_password\"?\s*[:=]\s*\"?)[^\"\s,}]+")


class VerifierError(Exception):
    """验证请求不可执行（路径越界/脚本缺失/配置缺失等，均 fail-closed）。"""


def _redact_text(text: str) -> str:
    return _SECRET_RE.sub(r"\1***", text)


def resolve_workspace(raw: Any) -> Path:
    """工作空间锚点（内核/管道注入的权威值）；缺失或非绝对路径即拒绝。"""
    value = str(raw or "").strip()
    if not value:
        raise VerifierError("缺少 workspace 锚点（工具须由内核/管道注入工作空间）")
    path = Path(value)
    if not path.is_absolute():
        raise VerifierError(f"workspace 不是绝对路径: {value}")
    if not path.is_dir():
        raise VerifierError(f"workspace 目录不存在: {value}")
    return path.resolve()


def resolve_plugin_dir(workspace: Path, raw: Any) -> Path:
    """插件目录 = 工作空间内相对路径；绝对路径、`..`、符号链接逃逸一律拒绝。"""
    value = str(raw or "").strip()
    if not value:
        raise VerifierError("缺少 plugin_path（示例: plugins/shared/my_plugin）")
    candidate = Path(value)
    if candidate.is_absolute():
        raise VerifierError(f"plugin_path 必须是工作空间相对路径: {value}")
    if any(part == ".." for part in candidate.parts):
        raise VerifierError(f"plugin_path 不得含 `..`: {value}")
    target = (workspace / candidate).resolve()
    if not target.is_relative_to(workspace):
        raise VerifierError(f"plugin_path 越出工作空间: {value}")
    if not (target / "plugin.json").is_file():
        raise VerifierError(f"插件目录缺 plugin.json: {target}")
    return target


def read_plugin_manifest(plugin_dir: Path) -> dict[str, Any]:
    """读 plugin.json；非对象/缺 id/id 形态非法一律拒绝（id 参与 scratch 命名）。"""
    try:
        manifest = json.loads((plugin_dir / "plugin.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise VerifierError(f"plugin.json 不可读或不可解析: {exc}") from exc
    if not isinstance(manifest, dict):
        raise VerifierError("plugin.json 顶层必须是对象")
    plugin_id = manifest.get("id")
    if not isinstance(plugin_id, str) or _PLUGIN_ID_RE.match(plugin_id) is None:
        raise VerifierError(f"plugin.json id 缺失或形态非法: {plugin_id!r}")
    return manifest


def _app_roots_from_process_ancestry() -> list[Path]:
    """从进程祖先的 exe 路径反推安装根（Tier2 用户副本场景的关键）。

    Tier2 插件运行在 `<USER_ROOT>/plugins/...`，按插件位置上溯得到的是用户根
    （无 kernel/skills/config 安装位）；而 sidecar 的祖先进程之一就是内核
    （`<resources>/kernel/agentos-kernel.exe` 或 dev `kernel/target/<profile>/`）。
    逐级祖先前溯、对每个 exe 的父目录链取候选，调用方按"含固定脚本"判定。
    """
    try:
        import psutil  # noqa: PLC0415 - 仅本函数需要，缺依赖时静默回退
    except ImportError:
        return []
    roots: list[Path] = []
    try:
        proc: Any = psutil.Process()
        for _ in range(5):  # sidecar → (可选 host) → kernel
            proc = proc.parent()
            if proc is None:
                break
            exe = str(proc.exe() or "")
            if not exe:
                continue
            roots.extend(Path(exe).parents[:6])
    except (psutil.Error, OSError) as exc:
        logger.debug("[plugin_verifier] 进程祖先安装根推导失败: %s", exc)
    return roots


def resolve_app_root(explicit: Path | None = None) -> Path:
    """固定脚本所在安装根。

    显式参数 = 测试注入（只认显式，不回落）；生产解析链：
    环境变量 `AGENTOS_APP_ROOT` > 进程祖先内核 exe 推导（Tier2 用户副本）>
    本插件安装位置（Tier1/内置副本）。首个含固定脚本的候选胜出；全落空拒绝。
    """
    if explicit is not None:
        root = explicit.resolve()
        if not (root / _VERIFY_HOST_REL).is_file():
            raise VerifierError(f"验证脚本不在安装位内: {root / _VERIFY_HOST_REL}")
        return root
    candidates: list[Path] = []
    env_root = os.environ.get("AGENTOS_APP_ROOT", "").strip()
    if env_root:
        candidates.append(Path(env_root))
    candidates.extend(_app_roots_from_process_ancestry())
    candidates.append(_APP_ROOT)
    seen: set[str] = set()
    for candidate in candidates:
        root = candidate.resolve()
        if str(root) in seen:
            continue
        seen.add(str(root))
        if (root / _VERIFY_HOST_REL).is_file():
            return root
    raise VerifierError(
        "验证脚本不在安装位内（已试: " + ", ".join(sorted(seen)) + "）"
    )


def _normalized_sha256(path: Path) -> str:
    """EOL 归一后的 SHA-256（Windows/Linux 检出字节一致，pin 不因 CRLF 假红）。"""
    data = path.read_bytes().replace(b"\r\n", b"\n")
    return hashlib.sha256(data).hexdigest()


def load_script_pins(pin_file: Path | None = None) -> dict[str, str]:
    """读脚本 pin 表（相对安装根的路径 → 归一化 sha256）。"""
    path = pin_file or _PIN_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise VerifierError(f"脚本 pin 表不可读: {path}: {exc}") from exc
    files = data.get("files") if isinstance(data, dict) else None
    if not isinstance(files, dict) or not files:
        raise VerifierError(f"脚本 pin 表形态非法: {path}")
    return {str(k): str(v) for k, v in files.items()}


def check_script_pins(
    app_root: Path, pins: dict[str, str] | None = None
) -> dict[str, str]:
    """逐脚本校验 pin；缺失/不符即拒绝（脚本锁定，改动须提审 pin 表）。"""
    table = pins if pins is not None else load_script_pins()
    seen: dict[str, str] = {}
    for rel, expected in table.items():
        script = app_root / rel
        if not script.is_file():
            raise VerifierError(f"固定脚本缺失: {script}")
        actual = _normalized_sha256(script)
        if actual != expected:
            raise VerifierError(
                f"固定脚本指纹不符（脚本被改动或 pin 未随提审更新）: {rel}"
            )
        seen[rel] = actual
    missing = [rel for rel in _PINNED_SCRIPTS if rel not in seen]
    if missing:
        raise VerifierError(f"脚本 pin 表未覆盖必需脚本: {missing}")
    return seen


def build_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """子进程环境白名单化：只留运行必需键，强制 UTF-8。"""
    source = os.environ if base is None else base
    env = {key: source[key] for key in _ENV_ALLOWED if source.get(key)}
    env.update(_ENV_FORCED)
    return env


def _first_file(candidates: list[Path]) -> Path | None:
    return next((path for path in candidates if path.is_file()), None)


def resolve_kernel_bin(app_root: Path) -> Path:
    """内核二进制：装机包 `<root>/kernel/` 优先，回落仓内 cargo 产物。

    仓内布局 = 独立 cargo 工作区 `kernel/target/<profile>/`；早期仓根
    `target/` 布局保留为兼容候选（不删，防旧工作区假红）。
    """
    names = ("agentos-kernel.exe", "agentos-kernel")
    candidates = [app_root / "kernel" / name for name in names]
    for profile in ("debug", "release"):
        candidates.extend(app_root / "kernel" / "target" / profile / name for name in names)
        candidates.extend(app_root / "target" / profile / name for name in names)
    found = _first_file(candidates)
    if found is None:
        raise VerifierError(
            f"找不到内核二进制（已试 {[str(c) for c in candidates]}）；先构建 scratch 内核"
        )
    return found


def resolve_config_root(app_root: Path) -> Path:
    """配置根（含 kernel/plugin_tiers.yaml）——内核启动守卫按它判 Tier 归属。"""
    config_root = app_root / "config"
    if not (config_root / "kernel" / "plugin_tiers.yaml").is_file():
        raise VerifierError(f"配置根缺 kernel/plugin_tiers.yaml: {config_root}")
    return config_root


def resolve_plugins_root(app_root: Path) -> Path:
    """内置插件根（验证实例的 tier1/shared 基座，取安装位而非 agent 工作区副本）。"""
    plugins_root = app_root / "plugins" / "shared"
    if not plugins_root.is_dir():
        raise VerifierError(f"内置插件根不存在: {plugins_root}")
    return plugins_root


def resolve_frontend_dist(app_root: Path) -> Path | None:
    """装机/仓内已构建前端 dist；缺失返回 None（脚本自行回退 workspace）。"""
    dist = app_root / "frontend" / "dist"
    return dist if (dist / "index.html").is_file() else None


def normalize_scope(raw: Any) -> str:
    scope = str(raw or "backend").strip().lower()
    if scope not in _SCOPES:
        raise VerifierError(f"scope 非法（backend/frontend）: {raw!r}")
    return scope


def normalize_timeout(raw: Any) -> int:
    try:
        value = int(raw if raw is not None else _DEFAULT_TIMEOUT_S)
    except (TypeError, ValueError) as exc:
        raise VerifierError(f"timeout_s 非法: {raw!r}") from exc
    if value < _MIN_TIMEOUT_S or value > _MAX_TIMEOUT_S:
        raise VerifierError(f"timeout_s 超出 {_MIN_TIMEOUT_S}..{_MAX_TIMEOUT_S}: {value}")
    return value


def build_command(
    *,
    app_root: Path,
    plugin_dir: Path,
    workspace: Path,
    scratch_root: Path,
    kernel_bin: Path,
    config_root: Path,
    plugins_root: Path,
    scope: str,
    frontend_dist: Path | None = None,
) -> list[str]:
    """固定脚本 + 固定参数位；命令面不接受调用方自由拼接。"""
    command = [
        sys.executable,
        str(app_root / _VERIFY_HOST_REL),
        str(plugin_dir),
        "--workspace",
        str(workspace),
        "--kernel-bin",
        str(kernel_bin),
        "--config-root",
        str(config_root),
        "--base-plugins-root",
        str(plugins_root),
        "--scratch-root",
        str(scratch_root),
    ]
    if scope == "frontend":
        command.append("--frontend")
        if frontend_dist is not None:
            command.extend(["--frontend-dist", str(frontend_dist)])
    return command


def run_command(
    command: list[str], *, cwd: Path, env: dict[str, str], timeout_s: int
) -> tuple[int, str, str]:
    """跑固定脚本；超时杀整棵进程树并返回 124（不把超时当成功）。"""
    proc = subprocess.Popen(  # noqa: S603 - 命令面全部由本模块构造，无 shell
        command,
        cwd=str(cwd),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout_s)
        return proc.returncode, stdout or "", stderr or ""
    except subprocess.TimeoutExpired:
        from _root_api import kill_process_tree  # noqa: PLC0415 - 仅超时路径需要

        failures = kill_process_tree(proc.pid)
        try:
            stdout, stderr = proc.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate()
        detail = f"验证超时（>{timeout_s}s），已杀进程树"
        if failures:
            detail += f"；清理失败: {failures}"
        return 124, stdout or "", ((stderr or "") + "\n" + detail).strip()


def _drop_view_junctions(scratch_root: Path) -> None:
    """先摘装载视图的目录链接（junction/symlink），再删 scratch。

    视图子项是指向 staging 插件的目录链接；直接 rmtree 在部分 Python/
    文件系统组合上会把链接当目录递归（有删到被测插件原件的风险）。
    已知布局：`<scratch>/lane/.verify_tier{1,2}_view/<plugin_id>`。
    """
    lane = scratch_root / "lane"
    if not lane.is_dir():
        return
    for view in lane.glob(".verify_*_view"):
        for child in view.iterdir():
            try:
                child.rmdir() if child.is_dir() else child.unlink()
            except OSError:  # 链接仍被占用：交给 rmtree 重试/脱敏兜底
                logger.debug("[plugin_verifier] 视图链接摘除失败: %s", child)
        try:
            view.rmdir()
        except OSError:
            logger.debug("[plugin_verifier] 视图目录删除失败: %s", view)


def _cleanup_scratch(scratch_root: Path) -> list[str]:
    """删 scratch（含 instance.yaml 明文口令）；Windows 文件锁按短退避重试。

    返回失败清单（空 = 已清）。仍失败时把残留 instance.yaml 中的口令字段
    就地脱敏，至少不留可复用凭证；目录本身交调用方报错可见。
    """
    _drop_view_junctions(scratch_root)
    for attempt in range(5):
        shutil.rmtree(scratch_root, ignore_errors=True)
        if not scratch_root.exists():
            try:
                scratch_root.parent.rmdir()  # 空 .plugin_verify 父目录一并收走
            except OSError:
                pass  # 父目录还有并发实例：保留
            return []
        time.sleep(0.5 * (attempt + 1))
    for instance_file in scratch_root.rglob("instance.yaml"):
        try:
            text = instance_file.read_text(encoding="utf-8")
            instance_file.write_text(_redact_text(text), encoding="utf-8", newline="\n")
        except OSError:  # 锁未释放：口令脱敏失败也不能阻断清理上报
            logger.warning("[plugin_verifier] 残留 instance.yaml 脱敏失败: %s", instance_file)
    leftovers = [str(path) for path in scratch_root.rglob("*")][:10]
    return leftovers or [str(scratch_root)]


def scrub_evidence(evidence: Any) -> Any:
    """递归脱敏证据（admin_password → ***）；返回新对象，不改原引用。"""
    if isinstance(evidence, dict):
        return {
            key: "***" if key == "admin_password" and value is not None else scrub_evidence(value)
            for key, value in evidence.items()
        }
    if isinstance(evidence, list):
        return [scrub_evidence(item) for item in evidence]
    return evidence


def read_evidence(plugin_dir: Path) -> dict[str, Any] | None:
    """读脚本落盘的合并证据；缺失/损坏返回 None（由调用方按失败面处理）。"""
    path = plugin_dir / _EVIDENCE_NAME
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("[plugin_verifier] 证据 JSON 不可解析: %s", path, exc_info=True)
        return None
    return data if isinstance(data, dict) else None


def _screenshots(evidence: dict[str, Any] | None) -> list[str]:
    frontend = (evidence or {}).get("frontend")
    shots = frontend.get("screenshots") if isinstance(frontend, dict) else None
    if not isinstance(shots, list):
        return []
    return [str(item) for item in shots if isinstance(item, str)]


def _checks(evidence: dict[str, Any] | None) -> list[dict[str, Any]]:
    checks = (evidence or {}).get("checks")
    if not isinstance(checks, list):
        return []
    return [item for item in checks if isinstance(item, dict)]


def run_plugin_verification(
    workspace_raw: Any,
    plugin_path: Any,
    scope: Any = "backend",
    timeout_s: Any = _DEFAULT_TIMEOUT_S,
    *,
    app_root: Path | None = None,
) -> dict[str, Any]:
    """验证一个工作空间内插件；返回结构化摘要（证据文件路径 + 断言明细）。

    app_root 仅测试注入（生产取本插件安装根）。
    """
    workspace = resolve_workspace(workspace_raw)
    plugin_dir = resolve_plugin_dir(workspace, plugin_path)
    manifest = read_plugin_manifest(plugin_dir)
    plugin_id = str(manifest["id"])
    scope = normalize_scope(scope)
    timeout = normalize_timeout(timeout_s)
    root = resolve_app_root(app_root)
    check_script_pins(root)
    kernel_bin = resolve_kernel_bin(root)
    config_root = resolve_config_root(root)
    plugins_root = resolve_plugins_root(root)
    frontend_dist = resolve_frontend_dist(root) if scope == "frontend" else None
    scratch_root = workspace / ".plugin_verify" / f"{plugin_id}-{uuid.uuid4().hex[:8]}"

    command = build_command(
        app_root=root,
        plugin_dir=plugin_dir,
        workspace=workspace,
        scratch_root=scratch_root,
        kernel_bin=kernel_bin,
        config_root=config_root,
        plugins_root=plugins_root,
        scope=scope,
        frontend_dist=frontend_dist,
    )
    env = build_env()
    try:
        exit_code, stdout, stderr = run_command(
            command, cwd=workspace, env=env, timeout_s=timeout
        )
        evidence = read_evidence(plugin_dir)
        if evidence is not None:
            # 双保险：即使脚本回归把口令写进证据，落盘副本也统一脱敏。
            (plugin_dir / _EVIDENCE_NAME).write_text(
                json.dumps(scrub_evidence(evidence), ensure_ascii=False, indent=1) + "\n",
                encoding="utf-8",
                newline="\n",
            )
        checks = _checks(evidence)
        all_pass = bool(evidence.get("all_pass")) if evidence else False
        status = "timeout" if exit_code == 124 else ("completed" if exit_code == 0 else "failed")
        error = None
        if exit_code != 0:
            tail = _redact_text((stderr or stdout).strip())[-1200:]
            error = tail or f"验证脚本退出码 {exit_code}"
        elif not evidence:
            error = "验证脚本未产出证据文件（verify_host_evidence.json）"
        cleanup_failures = _cleanup_scratch(scratch_root)
        if cleanup_failures:
            note = f"scratch 清理失败（残留 {len(cleanup_failures)} 项，实例口令已就地脱敏）"
            error = f"{error}；{note}" if error else note
        return {
            "success": exit_code == 0 and all_pass,
            "status": status,
            "plugin_id": plugin_id,
            "scope": scope,
            "all_pass": all_pass,
            "checks": checks,
            "screenshots": _screenshots(evidence),
            "evidence_path": str(plugin_dir / _EVIDENCE_NAME) if evidence else None,
            "error": error,
        }
    finally:
        # 前端探针的 probe_config.json 含明文 admin 口令；scratch 的 instance.yaml
        # 同理。两者都在 agent 可读面内，验证结束即删，不留下可复用凭证。
        (plugin_dir / "verify_frontend_evidence" / "probe_config.json").unlink(
            missing_ok=True
        )
        _cleanup_scratch(scratch_root)
