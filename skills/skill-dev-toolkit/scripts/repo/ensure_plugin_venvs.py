"""插件 venv 供给与健康断言（dev launcher Step 2 消费；装机态内核 autoprovision 同语义的仓侧前置）。

缺陷背景（装机 2026-10-18 三犯实证）：旧 Step 2 门控=「.venv 目录存在即跳过」
且 uv sync 输出静默（>nul 2>&1）、零重建后断言——部分完成/失败的 sync 留下残缺
.venv（agentos_plugin_sdk editable 未装）后门控永不重刷，坏态经包内携带/junction
传播到装机，合宿组全灭 50 分钟（ModuleNotFoundError: agentos_plugin_sdk）。

本脚本语义：
- 「venv 可用」= 解释器在 AND（pyproject 未声明 SDK OR SDK 可 import）——
  仅目录存在≠可用；
- 不健康 → `uv sync --project <dir>` 补装 → 再断言；仍坏 → 完整重跑一轮 →
  再断言（两轮上限，防 crash-loop 式空转）；
- 目标枚举与 launcher/内核选形同规则：`_host` 共享宿主恒首个（合宿全体
  spawn 的前置）；plugin.json 声明 host_group 的成员跳过（自身 .venv 运行期
  零消费）；有 plugin.json + pyproject.toml 的独立 sidecar 入选；
- 单插件失败不阻断其余；任何 unresolved → 退出码 1（launcher 打 WARN 不中断
  启动——供给面失败语义与现状一致，spawn 期保持 fail-closed）。

用法：
  python scripts/ensure_plugin_venvs.py          # 供给+断言（人类可读输出）
  python scripts/ensure_plugin_venvs.py --json   # 机器可读汇总
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SHARED_ROOT = REPO / "plugins" / "shared"

SDK_MODULE = "agentos_plugin_sdk"
SDK_DIST_NAME = "agentos-plugin-sdk"

# 单次 uv sync / import 探测超时（秒）。300 对齐内核 autoprovision 的单插件上限
# （冷缓存装 litellm 级依赖可到分钟级）；探测 60s 富余（import 秒级完成）。
SYNC_TIMEOUT_SECS = 300
IMPORT_TIMEOUT_SECS = 60


def venv_interpreter_path(plugin_dir: Path) -> Path:
    """venv 解释器路径（Windows `Scripts/python.exe`，其余 `bin/python`）。"""
    exe = "python.exe" if sys.platform == "win32" else "python"
    return plugin_dir / ".venv" / ("Scripts" if sys.platform == "win32" else "bin") / exe


def pyproject_declares_sdk(plugin_dir: Path) -> bool:
    """pyproject.toml 是否声明 SDK 依赖（文本包含扫描——声明名唯一，无需 TOML 解析）。"""
    pp = plugin_dir / "pyproject.toml"
    try:
        return SDK_DIST_NAME in pp.read_text(encoding="utf-8")
    except OSError:
        return False


def is_manifest_cohost_member(plugin_dir: Path) -> bool:
    """plugin.json 声明 host_group 即合宿成员（与内核 is_cohost_member 同规则）。"""
    manifest = plugin_dir / "plugin.json"
    try:
        meta = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return bool(meta.get("host_group"))


def iter_targets(plugins_root: Path) -> list[Path]:
    """供给目标集：`_host` 恒首个，其余为有 plugin.json+pyproject 的独立插件（定深一级，
    禁递归——dsh_adapter 存在自嵌套 junction 会挂死，与 bootstrap_plugin_envs 同纪律）。"""
    if not plugins_root.is_dir():
        return []
    targets: list[Path] = []
    for entry in sorted(plugins_root.iterdir()):
        if not entry.is_dir():
            continue
        if entry.name == "_host":
            targets.append(entry)
            continue
        if not (entry / "plugin.json").is_file():
            continue
        if not (entry / "pyproject.toml").is_file():
            continue
        if is_manifest_cohost_member(entry):
            continue
        targets.append(entry)
    # _host 排后但必须居首：合宿成员 spawn 只认共享 venv，供给序硬前置
    targets.sort(key=lambda p: p.name != "_host")
    return targets


def _probe_import(interpreter: Path, module: str) -> bool:
    """真解释器 import 探测：exit 0 = 可 import；任何失败（缺解释器/超时/非零）= False。"""
    try:
        done = subprocess.run(
            [str(interpreter), "-c", f"import {module}"],
            capture_output=True,
            timeout=IMPORT_TIMEOUT_SECS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return done.returncode == 0


def sdk_importable(
    interpreter: Path,
    runner: Callable[[Path], bool] | None = None,
) -> bool:
    """SDK 可 import 判据（runner 注入便于测试；生产走真子进程）。"""
    if runner is not None:
        return runner(interpreter)
    return _probe_import(interpreter, SDK_MODULE)


def uv_sync(project_dir: Path) -> tuple[bool, str]:
    """`uv sync --project <dir>`（不加 --frozen/--inexact：launcher 语义=按锁完整同步）。"""
    try:
        done = subprocess.run(
            ["uv", "sync", "--project", str(project_dir)],
            capture_output=True,
            text=True,
            timeout=SYNC_TIMEOUT_SECS,
            check=False,
        )
    except FileNotFoundError:
        return False, "uv 不在 PATH"
    except subprocess.TimeoutExpired:
        return False, f"uv sync 超时（>{SYNC_TIMEOUT_SECS}s）"
    if done.returncode == 0:
        return True, ""
    return False, (done.stderr or done.stdout or "").strip()[-500:]


def ensure_plugin_venv(
    plugin_dir: Path,
    sync_runner: Callable[[Path], tuple[bool, str]] | None = None,
    import_runner: Callable[[Path], bool] | None = None,
) -> tuple[str, str]:
    """单插件 venv 供给与健康断言。

    Returns:
        (status, detail)：status ∈ {"ok", "repaired", "broken"}；detail 为
        broken 时的可读原因（uv 报错截断/断言未过说明）。

    语义：不健康即补装并再断言，仍坏完整重跑一轮再断言（两轮上限）。sync 进程
    失败不提前退出——失败轮同样计数，重试交由第二轮（网络抖动自愈的最小形态）。
    """
    sync = sync_runner if sync_runner is not None else uv_sync
    interpreter = venv_interpreter_path(plugin_dir)
    needs_sdk = pyproject_declares_sdk(plugin_dir)

    def healthy() -> bool:
        if not interpreter.is_file():
            return False
        if not needs_sdk:
            return True
        return sdk_importable(interpreter, runner=import_runner)

    if healthy():
        return "ok", ""

    detail = ""
    for round_no in (1, 2):
        ok, err = sync(plugin_dir)
        if healthy():
            return "repaired", "" if ok else f"（第 {round_no} 轮 sync 报错但断言已过）"
        detail = err or f"补装后第 {round_no} 轮断言仍未通过（SDK={'需' if needs_sdk else '不需'}）"
    return "broken", detail


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true", help="输出 JSON 汇总")
    parser.add_argument(
        "--root",
        type=Path,
        default=SHARED_ROOT,
        help="插件根（默认 <repo>/plugins/shared；测试/非标布局可指）",
    )
    args = parser.parse_args()

    results: list[dict[str, str]] = []
    for target in iter_targets(args.root):
        status, detail = ensure_plugin_venv(target)
        results.append({"dir": str(target), "status": status, "detail": detail})
        mark = {"ok": "[OK]", "repaired": "[FIXED]", "broken": "[BROKEN]"}[status]
        print(f"  {mark} {target.name}" + (f" — {detail}" if detail else ""))

    broken = [r for r in results if r["status"] == "broken"]
    repaired = [r for r in results if r["status"] == "repaired"]
    if args.json:
        print(json.dumps({"results": results, "broken": len(broken)}, ensure_ascii=False))
    print(
        f"[venv] 目标 {len(results)}：健康 {len(results) - len(broken) - len(repaired)}"
        f" / 补装成功 {len(repaired)} / 仍故障 {len(broken)}"
    )
    if broken:
        print(
            "[WARN] 上述插件 venv 两轮补装后仍不可用——对应工具/合宿组将 502，请检查 uv 输出或网络后重跑",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
