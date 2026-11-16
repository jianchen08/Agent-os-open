#!/usr/bin/env python3
"""插件依赖审计（方案 §Phase1 第 1 件，原则 P1）：依赖分类 → 能力门禁表初版。

扫全量 ``plugins/**/pyproject.toml`` + ``uv.lock``，把每个插件锁定的依赖
集合分类为纯 Python / C 扩展（对照 **aarch64 manylinux/musllinux wheel
可得性**——手机 proot arm64 的可装判据），产出运行时能力门禁表
``config/plugins/capability_gates.yaml`` 初版（schema 契约见
``plugins/shared/capability_gates/gates.py``）。审计只产出**运行时降级
判定依据**，不产出裁剪清单：插件集合三端一致（P1），跑不动的插件运行时
打降级标记。

数据源（离线，零网络）：
- ``pyproject.toml``：插件身份（plugin.json id > pyproject name > 目录名）；
- ``uv.lock``：锁定事实源——``[[package]]`` 的 ``wheels[].url`` 是该版本
  实际发布的产物全集，wheel 平台标签决定分类，不靠包名猜测。

分类规则（按锁内 wheel 文件名，保守序）：
- 含 ``*-none-any.whl`` → ``pure``（纯 Python；带可选加速轮的包手机装纯轮）；
- 含 aarch64 × (manylinux|musllinux) 轮 → ``cext-ok``（C 扩展，手机可装）；
- 其余有轮（仅 x86_64 linux / win / mac，无 aarch64 linux）→ ``cext-blocked``；
- 零 wheel（仅 sdist）→ ``sdist-only``（分类未知：不入门禁，报告列明——
  门禁只认证据，与解析器 fail-open 默认同律）。

平台排除：某包的全部引用边都带 Linux 外平台 marker（如 win32 专属依赖）
时，它在手机端永不安装，不入分类与门禁（marker 经 packaging 按
linux/aarch64/CPython 环境求值）。

门禁合成：插件锁内存在 ``cext-blocked`` 包 → ``{min_profile: desktop,
degrade_reason: "C 扩展依赖缺 aarch64 wheel: <包列表>"}``；否则不入表
（fail-open：默认不降级）。workspace 成员（插件自身 / editable SDK）不入
分类。

用法::

    python scripts/audit_plugin_deps.py               # 审计 + 写 gates yaml + 打统计
    python scripts/audit_plugin_deps.py --dry-run     # 只打报告，不写文件
    python scripts/audit_plugin_deps.py --out <path>  # 自定输出路径
"""

from __future__ import annotations

import argparse
import os
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PLUGINS_ROOT = ROOT / "plugins"
DEFAULT_OUT = ROOT / "config" / "plugins" / "capability_gates.yaml"

# 门禁表固定语义：缺 aarch64 wheel 的 C 扩展插件在手机档降级，桌面/云可用。
BLOCKED_MIN_PROFILE = "desktop"
# workspace 成员的 source 形态（非 registry 安装，不可按 wheel 判）。
_WORKSPACE_SOURCE_KEYS = frozenset({"editable", "virtual", "path", "directory"})

# 剪枝目录：venv 族与构建产物不属插件源（与 check_config_direct_reads 的
# EXCLUDE_DIRS 同款惯例，含 .venv-hindsight 这类命名变体）。
_PRUNE_DIRS = frozenset(
    {".venv", "venv", ".venv-hindsight", ".ai_workspaces", "node_modules", "__pycache__", ".git", "target", "runtime"}
)


class AuditError(Exception):
    """审计输入损坏（锁文件/清单不可解析），fail-fast 载体。"""


# ── 数据模型 ─────────────────────────────────────────────


@dataclass
class PluginAudit:
    """单插件审计结果：身份、锁定包分类与门禁素材。"""

    plugin_id: str
    dir_rel: str
    has_lock: bool
    has_pyproject: bool
    pure: list[str] = field(default_factory=list)
    cext_ok: list[str] = field(default_factory=list)
    cext_blocked: list[str] = field(default_factory=list)
    sdist_only: list[str] = field(default_factory=list)

    @property
    def is_gated(self) -> bool:
        return bool(self.cext_blocked)


# ── 扫描与解析 ───────────────────────────────────────────


def iter_plugin_dirs(plugins_root: Path) -> list[Path]:
    """收集含 pyproject.toml 的插件/宿主/SDK 目录（剪枝重目录，按路径排序）。

    遍历期就地剪枝（os.walk 改 dirnames）而非 rglob 后过滤：插件 .venv
    动辄数万文件，先下钻再剪是无谓的扫描大头。
    """
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(plugins_root):
        dirnames[:] = [d for d in dirnames if d not in _PRUNE_DIRS]
        if "pyproject.toml" in filenames:
            found.append(Path(dirpath))
    return sorted(found)


def resolve_plugin_id(plugin_dir: Path, plugins_root: Path) -> str:
    """插件身份：plugin.json id > pyproject [project].name > 目录名。"""
    manifest = plugin_dir / "plugin.json"
    if manifest.is_file():
        import json

        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise AuditError(f"{manifest} 不可解析：{exc}") from exc
        plugin_id = data.get("id")
        if isinstance(plugin_id, str) and plugin_id.strip():
            return plugin_id
    pyproject = plugin_dir / "pyproject.toml"
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        name = data.get("project", {}).get("name")
    except (tomllib.TOMLDecodeError, OSError) as exc:
        raise AuditError(f"{pyproject} 不可解析：{exc}") from exc
    if isinstance(name, str) and name.strip():
        return name
    return plugin_dir.relative_to(plugins_root).as_posix()


def load_lock_packages(lock_path: Path) -> list[dict[str, Any]]:
    """读 uv.lock 的 [[package]] 数组（不可解析即 fail-fast）。"""
    try:
        data = tomllib.loads(lock_path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError) as exc:
        raise AuditError(f"{lock_path} 不可解析：{exc}") from exc
    packages = data.get("package", [])
    if not isinstance(packages, list):
        raise AuditError(f"{lock_path} package 段不是数组")
    return packages


def collect_edge_markers(packages: list[dict[str, Any]]) -> dict[str, list[str | None]]:
    """依赖边 marker 收集：{被依赖包名: [marker 或 None(无边)]}（全锁并集）。"""
    edges: dict[str, list[str | None]] = {}
    for pkg in packages:
        deps = pkg.get("dependencies", [])
        if not isinstance(deps, list):
            continue
        for dep in deps:
            if not isinstance(dep, dict) or not isinstance(dep.get("name"), str):
                continue
            edges.setdefault(dep["name"], []).append(dep.get("marker"))
    return edges


def _marker_environment() -> dict[str, str]:
    """手机档求值环境：Linux aarch64 CPython（proot Ubuntu 口径）。"""
    return {
        "implementation_name": "cpython",
        "platform_python_implementation": "CPython",
        "os_name": "posix",
        "sys_platform": "linux",
        "platform_system": "Linux",
        "platform_machine": "aarch64",
        "python_version": "3.11",
        "python_full_version": "3.11.9",
        "platform_release": "",
        "platform_version": "",
        "implementation_version": "3.11.9",
        "extra": "",
    }


def installable_on_phone(name: str, edge_markers: list[str | None]) -> tuple[bool, str | None]:
    """包在手机端是否会被安装：任一引用边无 marker 或求值为 True 即可装。

    返回 (可装, 求值失败 marker 原文)——marker 畸形 = 无法证明排除手机，
    保守按可装参与分类（blocked 包仍会正确进门禁，ok 包不加假门禁），
    原文交上层报告留痕，不留静默。
    """
    if not edge_markers:
        return True, None
    from packaging.markers import InvalidMarker, Marker

    env = _marker_environment()
    for raw in edge_markers:
        if raw is None:
            return True, None
        try:
            if Marker(raw).evaluate(env):
                return True, None
        except InvalidMarker as exc:
            return True, f"{raw}（{exc}）"
    return False, None


def classify_wheels(wheel_urls: list[str]) -> str:
    """按锁定 wheel 文件名分类：pure / cext-ok / cext-blocked / sdist-only。"""
    if not wheel_urls:
        return "sdist-only"
    names = [url.rsplit("/", 1)[-1] for url in wheel_urls]
    if any("-none-any.whl" in n for n in names):
        return "pure"
    has_aarch64_linux = any("aarch64" in n and ("manylinux" in n or "musllinux" in n) for n in names)
    if has_aarch64_linux:
        return "cext-ok"
    # 有平台轮但无 aarch64 linux 轮：C 扩展（或平台专属包）手机端不可装。
    return "cext-blocked"


def audit_plugin_dir(plugin_dir: Path, plugins_root: Path, broken_markers: dict[str, str]) -> PluginAudit:
    """审计单目录：锁内全部 registry 包按 wheel 分类，平台排除后归入四桶。"""
    plugin_id = resolve_plugin_id(plugin_dir, plugins_root)
    rel = plugin_dir.relative_to(plugins_root).as_posix()
    lock = plugin_dir / "uv.lock"
    audit = PluginAudit(plugin_id=plugin_id, dir_rel=rel, has_lock=lock.is_file(), has_pyproject=True)
    if not lock.is_file():
        return audit
    packages = load_lock_packages(lock)
    edges = collect_edge_markers(packages)
    for pkg in packages:
        if not isinstance(pkg, dict) or not isinstance(pkg.get("name"), str):
            continue
        source = pkg.get("source")
        if isinstance(source, dict) and (set(source) & _WORKSPACE_SOURCE_KEYS):
            continue  # workspace 成员（插件自身/editable SDK）不按 wheel 判
        name = pkg["name"]
        installable, broken = installable_on_phone(name, edges.get(name, []))
        if broken:
            broken_markers[name] = broken
        if not installable:
            continue
        wheels = pkg.get("wheels", [])
        urls = (
            [w["url"] for w in wheels if isinstance(w, dict) and isinstance(w.get("url"), str)]
            if isinstance(wheels, list)
            else []
        )
        category = classify_wheels(urls)
        bucket = {
            "pure": audit.pure,
            "cext-ok": audit.cext_ok,
            "cext-blocked": audit.cext_blocked,
            "sdist-only": audit.sdist_only,
        }[category]
        bucket.append(name)
    return audit


# ── 门禁合成与输出 ────────────────────────────────────────


def build_gates(audits: list[PluginAudit]) -> dict[str, dict[str, object]]:
    """审计结果 → 门禁表初版：仅 cext-blocked 插件入表（fail-open 默认）。"""
    gates: dict[str, dict[str, object]] = {}
    for audit in audits:
        if audit.is_gated:
            gates[audit.plugin_id] = {
                "min_profile": BLOCKED_MIN_PROFILE,
                "degrade_reason": f"C 扩展依赖缺 aarch64 wheel: {', '.join(sorted(audit.cext_blocked))}",
            }
    return gates


def render_gates_yaml(gates: dict[str, dict[str, object]]) -> str:
    """门禁表序列化（schema 注释头 + 稳定排序正文，幂等可重跑）。"""
    import yaml

    header = (
        "# 能力门禁表（原则 P1：插件集合三端一致，运行时降级不裁集合）。\n"
        "# schema：顶层 mapping plugin_id -> {min_profile, degrade_reason}\n"
        "#   min_profile ∈ {phone, desktop, cloud}（词表见 device_profile.TIERS）；\n"
        "#   degrade_reason 非空字符串（前端「此端降级」提示文案）。\n"
        "# 行为契约：本文件缺失 = 全量默认不降级（fail-open）；字段非法 = fail-closed 报错。\n"
        "# 产物：scripts/audit_plugin_deps.py 全量依赖审计初版（aarch64 wheel 可得性判据），\n"
        "# 手工可增补/修订——机器重跑会整体覆盖，修订请连同审计判据一起改。\n"
    )
    body = yaml.safe_dump(gates, allow_unicode=True, sort_keys=True, width=100)
    return header + "\n" + body


def print_report(audits: list[PluginAudit], broken_markers: dict[str, str]) -> None:
    """统计报告（落台账口径：纯 Py / C 扩展各几件 + 门禁件数）。"""
    with_lock = [a for a in audits if a.has_lock]
    no_lock = [a for a in audits if not a.has_lock]
    gated = [a for a in audits if a.is_gated]
    pure_only = [a for a in with_lock if a.pure and not a.cext_ok and not a.cext_blocked]
    cext_ok_only = [a for a in with_lock if a.cext_ok and not a.cext_blocked]
    mixed = [a for a in with_lock if a.cext_ok and a.cext_blocked]
    sdist_unknown = [a for a in with_lock if a.sdist_only]
    unique_pure = {p for a in with_lock for p in a.pure}
    unique_ok = {p for a in with_lock for p in a.cext_ok}
    unique_blocked = {p for a in with_lock for p in a.cext_blocked}
    print("== 插件依赖审计统计 ==")
    print(f"扫描目录（含 pyproject.toml）: {len(audits)}")
    print(f"带 uv.lock: {len(with_lock)} | 无锁（纯 stdlib/未锁）: {len(no_lock)}")
    if no_lock:
        print(f"  无锁目录: {', '.join(a.dir_rel for a in no_lock)}")
    print(
        f"插件级分类: 纯 Python {len(pure_only)} | C 扩展手机可装 {len(cext_ok_only)}"
        f" | 混合(可装+阻塞) {len(mixed)} | 命中门禁 {len(gated)}"
    )
    print(f"  纯 Python: {', '.join(sorted(a.plugin_id for a in pure_only)) or '（无）'}")
    print(f"  门禁命中（cext-blocked）: {', '.join(sorted(a.plugin_id for a in gated)) or '（无）'}")
    print(f"  sdist-only 分类未知: {', '.join(sorted(a.plugin_id for a in sdist_unknown)) or '（无）'}")
    print(
        f"包级去重: pure {len(unique_pure)} | cext-ok(aarch64 可得) {len(unique_ok)}"
        f" | cext-blocked {len(unique_blocked)}"
    )
    if unique_blocked:
        print(f"  blocked 包: {', '.join(sorted(unique_blocked))}")
    if broken_markers:
        print("marker 求值失败（保守按可装处理，请人工复核）:")
        for name, raw in sorted(broken_markers.items()):
            print(f"  {name}: {raw}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="插件依赖审计 → capability_gates.yaml 初版")
    parser.add_argument("--plugins-root", type=Path, default=DEFAULT_PLUGINS_ROOT)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--dry-run", action="store_true", help="只打报告，不写 gates yaml")
    args = parser.parse_args(argv)

    plugin_dirs = iter_plugin_dirs(args.plugins_root)
    if not plugin_dirs:
        print(f"未在任何目录找到 pyproject.toml：{args.plugins_root}", file=sys.stderr)
        return 1
    broken_markers: dict[str, str] = {}
    audits = [audit_plugin_dir(d, args.plugins_root, broken_markers) for d in plugin_dirs]
    gates = build_gates(audits)
    print_report(audits, broken_markers)
    if args.dry_run:
        print(f"[dry-run] 跳过写出 {args.out}")
        return 0
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(render_gates_yaml(gates), encoding="utf-8")
    print(f"gates yaml 已写出：{args.out}（{len(gates)} 条门禁）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
