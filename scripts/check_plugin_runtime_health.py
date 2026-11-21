#!/usr/bin/env python3
"""插件运行时健康检查（装机/部署态执法位——CI 设计缺口修复 2026-10-29）。

背景（2026-10-29 装机实证）：内核重启后插件从 resources 副本拉起（无 venv），
`No module named 'agentos_plugin_sdk'` 一片；llm_service 切用户空间 venv 后
litellm 进程内半初始化（`has no attribute 'Router'`）——/health 冒烟全绿，
CI 全绿，装机照样翻车。根因两类：(1) venv/SDK 部署态损坏；(2) editable
安装指向检查根之外（第三犯：装机 venv 的 SDK editable 指向开发仓，工作区
还原即悬空）。/health 冒烟看不见这两类——本脚本是补上的执法位。

三类检查（可组合，全部通过退出 0，任一失败退出 1）：
  --plugins-root <dir>   扫描 Python 插件目录：venv 解释器在位性（缺=警告，
                         --strict-missing-venv 升级为失败）、SDK 可导入、
                         editable 安装指向越界。host_group 合宿插件跳过
                         （解释器随组宿主 _host，COHOST 行列出）；
  --venv <dir>           检查指定 venv（不要求 plugin.json，如随包 _host）；
  --boot-log <file>      扫启动日志失败签名（缺 SDK / venv 解释器缺失 /
                         has no attribute——正常启动不应出现）；已知设计内
                         降级警告（KNOWN_DEGRADATION_MARKERS，如 isolation
                         合宿进程 ConfigCenter 探测必败→文件回退）豁免。

editable 越界判据：site-packages 内 ``__editable__*.pth``（路径行）与
``__editable__*finder*.py``（MAPPING 值）解析出的目标路径必须落在
``--allow-editable-under`` 根集合内（缺省=--plugins-root 自身）。

用法（CI 打包冒烟 · install_smoke_windows 第四段）::

    python scripts/check_plugin_runtime_health.py \\
        --boot-log <boot.log> --venv <resources>/plugins/shared/_host/.venv \\
        --allow-editable-under <resources>

用法（维护轮 · 重启后真机自检）::

    python scripts/check_plugin_runtime_health.py \\
        --plugins-root "$env:APPDATA\\agentos\\plugins" \\
        --boot-log <resources>\\kernel\\logs\\kernel.log.<启动日> \\
        --plugin-import llm=litellm
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

DEFAULT_SIGNATURES = (
    "No module named ",
    "venv 解释器缺失",
    "has no attribute ",
)
# 已知设计内降级（撞失败签名也豁免）：isolation 合宿进程无仓根，ConfigCenter
# 探测必败 → 警告后文件回退直读 <安装位>/config 真身（isolation/workspace.py
# _load_isolation_config 两级降级；「（文件回退）」二级失败才是真问题，不豁免）。
KNOWN_DEGRADATION_MARKERS = ("读取 isolation 配置失败（config_center）",)
SDK_CANARY = "import agentos_plugin_sdk"


# ---------- 纯逻辑（单测面） ----------


def parse_pth_targets(pth_text: str) -> list[str]:
    """从 .pth 文本提取路径目标行（import 行/空行/注释行不算）。"""
    targets: list[str] = []
    for line in pth_text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("import "):
            continue
        targets.append(stripped)
    return targets


def _extract_finder_mappings(finder_text: str) -> list[str]:
    """从 __editable__*_finder.py 提取 MAPPING 字典里的路径值（ast 精确解析）。"""
    import ast  # noqa: PLC0415

    targets: list[str] = []
    match = re.search(r"MAPPING\s*=\s*(\{[^}]*\})", finder_text, re.S)
    if not match:
        return targets
    try:
        mapping = ast.literal_eval(match.group(1))
    except (ValueError, SyntaxError):
        return targets
    if not isinstance(mapping, dict):
        return targets
    for value in mapping.values():
        if isinstance(value, str):
            targets.append(value)
    return targets


def _under(target: str, roots: list[Path]) -> bool:
    try:
        resolved = Path(target).resolve()
    except OSError:
        return False
    for root in roots:
        try:
            resolved.relative_to(root.resolve())
            return True
        except (ValueError, OSError):
            continue
    return False


def editable_violations(site_packages: Path, allowed_roots: list[Path]) -> list[str]:
    """site-packages 内 editable 安装指向越界的条目描述（空=全过）。"""
    violations: list[str] = []
    if not site_packages.is_dir():
        return violations
    for pth in sorted(site_packages.glob("__editable__*.pth")):
        try:
            text = pth.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            violations.append(f"editable pth 不可读 {pth.name}: {exc}")
            continue
        for target in parse_pth_targets(text):
            # 相对路径行（relocate 后形态）按 .pth 所在 site-packages 解析——
            # site.addpackage 同义；按 CWD 解析会对包内合法指向误报越界
            resolved_target = target if Path(target).is_absolute() else str(site_packages / target)
            if not _under(resolved_target, allowed_roots):
                violations.append(f"editable 越界 {pth.name} → {target}")
    for finder in sorted(site_packages.glob("__editable__*finder*.py")):
        try:
            text = finder.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            violations.append(f"editable finder 不可读 {finder.name}: {exc}")
            continue
        for target in _extract_finder_mappings(text):
            if not _under(target, allowed_roots):
                violations.append(f"editable finder 越界 {finder.name} → {target}")
    return violations


def find_venv_python(plugin_dir: Path) -> Path | None:
    """插件目录内 venv 解释器（Windows Scripts 优先，POSIX bin 兜底）。"""
    for rel in (".venv/Scripts/python.exe", ".venv/bin/python"):
        candidate = plugin_dir / rel
        if candidate.is_file():
            return candidate
    return None


def scan_boot_log(
    log_text: str,
    signatures: tuple[str, ...] = DEFAULT_SIGNATURES,
    degradation_markers: tuple[str, ...] = KNOWN_DEGRADATION_MARKERS,
) -> list[str]:
    """启动日志失败签名扫描：返回「行号: 片段」描述（空=干净）。

    命中已知设计内降级标记（degradation_markers）的行跳过——这些警告在装机态
    是常态而非故障，撞上失败签名（如 "No module named "）不作失败计。
    """
    hits: list[str] = []
    for lineno, line in enumerate(log_text.splitlines(), 1):
        if any(marker in line for marker in degradation_markers):
            continue
        if any(sig in line for sig in signatures):
            hits.append(f"行{lineno}: {line.strip()[:200]}")
    return hits


def plugin_cohost_group(plugin_dir: Path) -> str | None:
    """插件声明的合宿组（host_group）；无声明/清单不可解析 → None。

    合宿插件解释器随组宿主 _host（invoker venv.rs 解析链的组键形态），插件
    目录本身无 .venv 属正常部署态——插件级 venv/SDK/editable 检查不适用
    （组宿主 venv 由 --venv 车道单独执法，CI 打包冒烟即此配法）。
    """
    try:
        data = json.loads((plugin_dir / "plugin.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    group = data.get("host_group") if isinstance(data, dict) else None
    return group if isinstance(group, str) and group else None


def run_checks(
    plugins_root: Path | None,
    venvs: list[Path],
    boot_logs: list[Path],
    allowed_roots: list[Path],
    plugin_imports: dict[str, str],
    strict_missing_venv: bool,
    runner=None,
    signatures: tuple[str, ...] = DEFAULT_SIGNATURES,
) -> dict:
    """汇总三类检查 → 结构化报告（failures 非空 = 不健康）。"""
    runner = runner or _default_runner
    failures: list[str] = []
    warnings: list[str] = []
    cohosted: list[str] = []
    checked = {"venvs": 0, "plugins": 0, "boot_logs": 0, "cohosted": 0}

    if plugins_root is not None:
        for plugin_dir in _iter_plugin_dirs(plugins_root):
            group = plugin_cohost_group(plugin_dir)
            if group is not None:
                checked["cohosted"] += 1
                cohosted.append(f"{plugin_dir.name}（组 {group}）")
                continue
            checked["plugins"] += 1
            venv_python = find_venv_python(plugin_dir)
            if venv_python is None:
                msg = f"插件缺 venv 解释器 {plugin_dir.name}"
                (failures if strict_missing_venv else warnings).append(msg)
                continue
            checked["venvs"] += 1
            canaries = [SDK_CANARY]
            extra = plugin_imports.get(plugin_dir.name)
            if extra:
                canaries.append(f"import {extra}")
            ok, detail = runner(venv_python, canaries)
            if not ok:
                failures.append(f"venv 导入失败 {plugin_dir.name}: {detail}")
            sp = plugin_dir / ".venv" / "Lib" / "site-packages"
            for violation in editable_violations(sp, allowed_roots):
                failures.append(f"{plugin_dir.name}: {violation}")

    for venv_dir in venvs:
        checked["venvs"] += 1
        venv_python = find_venv_python(venv_dir)
        if venv_python is None:
            failures.append(f"指定 venv 缺解释器 {venv_dir}")
            continue
        ok, detail = runner(venv_python, [SDK_CANARY])
        if not ok:
            failures.append(f"venv 导入失败 {venv_dir.name}: {detail}")
        sp = _site_packages(venv_dir)
        for violation in editable_violations(sp, allowed_roots):
            failures.append(f"{venv_dir.name}: {violation}")

    for log_path in boot_logs:
        checked["boot_logs"] += 1
        try:
            text = log_path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            failures.append(f"boot 日志不可读 {log_path}: {exc}")
            continue
        for hit in scan_boot_log(text, signatures):
            failures.append(f"boot 日志失败签名 {log_path.name} {hit}")

    return {
        "ok": not failures,
        "failures": failures,
        "warnings": warnings,
        "cohosted": cohosted,
        "checked": checked,
    }


def _site_packages(venv_dir: Path) -> Path:
    for rel in ("Lib/site-packages", "lib/python*/site-packages"):
        candidates = sorted(venv_dir.glob(rel)) if "*" in rel else [venv_dir / rel]
        if candidates and candidates[0].is_dir():
            return candidates[0]
    return venv_dir / "Lib" / "site-packages"


def _iter_plugin_dirs(plugins_root: Path) -> list[Path]:
    """两层枚举（plugins/<id> 与 plugins/<组>/<id>，如 modes/mode_coding），
    剪枝 node_modules/.git（dsh_adapter 自嵌套 junction 会挂死裸 glob）。"""
    if not plugins_root.is_dir():
        return []
    dirs: list[Path] = []
    for child in sorted(plugins_root.iterdir()):
        if not child.is_dir() or child.name in ("node_modules", ".git"):
            continue
        if (child / "plugin.json").is_file():
            dirs.append(child)
        for grandchild in sorted(child.iterdir()):
            if (
                grandchild.is_dir()
                and grandchild.name not in ("node_modules", ".git", ".venv")
                and (grandchild / "plugin.json").is_file()
            ):
                dirs.append(grandchild)
    return dirs


def _default_runner(venv_python: Path, canaries: list[str], timeout_s: int = 120) -> tuple[bool, str]:
    code = "; ".join(canaries)
    try:
        proc = subprocess.run(
            [str(venv_python), "-c", code],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"解释器执行失败: {exc}"
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()
        return False, tail[-1][:200] if tail else f"rc={proc.returncode}"
    return True, ""


# ---------- CLI ----------


def main() -> int:
    parser = argparse.ArgumentParser(description="插件运行时健康检查（venv/SDK/editable/boot 日志）")
    parser.add_argument("--plugins-root", help="Python 插件集合根目录（可多次指定则取一次）")
    parser.add_argument("--venv", action="append", default=[], help="指定 venv 目录（可重复）")
    parser.add_argument("--boot-log", action="append", default=[], help="启动日志文件（可重复）")
    parser.add_argument(
        "--allow-editable-under",
        action="append",
        default=[],
        help="editable 指向允许根（可重复；缺省=--plugins-root）",
    )
    parser.add_argument(
        "--plugin-import",
        action="append",
        default=[],
        metavar="ID=MODULE",
        help="指定插件的额外导入金丝雀（可重复，如 llm=litellm）",
    )
    parser.add_argument("--strict-missing-venv", action="store_true", help="缺 venv 解释器升级为失败")
    parser.add_argument("--json", action="store_true", help="JSON 输出（机器消费）")
    args = parser.parse_args()

    plugins_root = Path(args.plugins_root).resolve() if args.plugins_root else None
    allowed = [Path(p).resolve() for p in args.allow_editable_under] or ([plugins_root] if plugins_root else [])
    plugin_imports = {}
    for item in args.plugin_import:
        if "=" not in item:
            parser.error(f"--plugin-import 形态应为 ID=MODULE：{item}")
        pid, module = item.split("=", 1)
        plugin_imports[pid] = module

    report = run_checks(
        plugins_root=plugins_root,
        venvs=[Path(v).resolve() for v in args.venv],
        boot_logs=[Path(p) for p in args.boot_log],
        allowed_roots=allowed,
        plugin_imports=plugin_imports,
        strict_missing_venv=args.strict_missing_venv,
    )

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=1))
    else:
        print(
            f"[plugin-runtime-health] 检查 venv={report['checked']['venvs']} "
            f"plugins={report['checked']['plugins']} boot_logs={report['checked']['boot_logs']} "
            f"cohosted={report['checked']['cohosted']}"
        )
        for item in report["cohosted"]:
            print(f"  COHOST {item}")
        for warning in report["warnings"]:
            print(f"  WARN {warning}")
        for failure in report["failures"]:
            print(f"  FAIL {failure}")
        print(f"[plugin-runtime-health] {'PASS' if report['ok'] else 'FAIL'}")

    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
