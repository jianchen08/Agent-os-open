"""打包产物结构校验：A3 选型 c——内置模式包瘦身共享宿主 _host venv。

对一个打包资源根（electron-builder 产物 resources/、安装目录 resources/、或
任意同构 staging 目录）断言共享 venv 结构（BUG-66 裁决：R261 选型 c，
R208 先例——合宿成员跳过自身 venv、进程从 plugins/shared/_host/.venv 拉起）：

  1. 共享合宿基座在位：plugins/shared/_host/{host.py, pyproject.toml}；
  2. 共享 venv 解释器在位：_host/.venv/{Scripts/python.exe | bin/python}
     （内核合宿 spawn fail-closed 只认它——HOST_VENV_MISSING 即全体 light
     成员 + 内置模式包不可用）；
  3. 内置模式包（plugins/shared/modes/*）不带任何自身 .venv；
  4. 一切声明 host_group 的合宿成员不带自身 .venv（运行期零消费，纯装机浪费）；
  5. 包内不出现 junction/symlink 形态的 .venv（Windows junction 存绝对路径，
     跨机必断——ADR 2026-09-07-plugin-venv-dedup）；
  6. 合宿成员 pyproject 声明的依赖已实装进包内 _host/.venv（extraResources
     整目录照搬 dev 磁盘 venv，dev 漏 sync 则装机版调用期 ModuleNotFoundError
     ——web_operate 缺 httpx 实证；解释器在位不等于依赖闭包完整）。

只读校验（无 --apply 语义）；independent 类插件带真实 .venv 属合法形态，
仅记 info。定深扫描（禁递归 glob，dsh_adapter 自嵌套 junction 会挂死）。

用法：
  python scripts/check_packaged_layout.py <resources根>
  python scripts/check_packaged_layout.py <resources根> --json   # 机器可读
退出码：0 = 结构合格；1 = 存在 error 级 finding。
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import stat
import sys
from pathlib import Path

SHARED_REL = Path("plugins") / "shared"
HOST_DIR_REL = SHARED_REL / "_host"
MODES_REL = SHARED_REL / "modes"

# 合宿基座必备文件（缺失 = 装机版合宿/mode pack 全体不可 spawn）
HOST_BASE_FILES = ("host.py", "pyproject.toml")

# 定深扫描布局（与 bootstrap_plugin_envs.py PLUGIN_GLOBS 同步；禁递归 glob）
PLUGIN_GLOBS = [
    "system/*",
    "tools/*",
    "pipeline/core/*",
    "pipeline/input/*",
    "pipeline/output/*",
    "modes/*",
    "db_admin",
    "metrics_admin",
    "user_admin",
]


@dataclasses.dataclass
class Finding:
    level: str  # "error" | "info"
    code: str
    path: str
    detail: str


def is_link(path: Path) -> bool:
    """junction 或 symlink（两者跨机/跨卷皆不可移植）。"""
    if path.is_symlink():
        return True
    if hasattr(os.path, "isjunction"):
        return os.path.isjunction(path)
    st = path.lstat()
    return bool(st.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def venv_interpreter(venv_dir: Path) -> Path | None:
    """uv venv 标准布局探测（同内核 find_venv_interpreter 口径，双平台）。"""
    for candidate in (
        venv_dir / "Scripts" / "python.exe",
        venv_dir / "Scripts" / "python",
        venv_dir / "bin" / "python",
    ):
        if candidate.is_file():
            return candidate
    return None


def manifest_host_group(plugin_dir: Path) -> str | None:
    """manifest 的 host_group 声明（解析失败按未声明处理，保守不误报）。"""
    import json

    manifest = plugin_dir / "plugin.json"
    if not manifest.is_file():
        return None
    try:
        meta = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    group = meta.get("host_group")
    return str(group) if group else None


def norm(name: str) -> str:
    """PEP 503 归一（与 bootstrap_plugin_envs 同口径）。"""
    import re

    return re.sub(r"[-_.]+", "-", name).lower()


def pyproject_dep_names(plugin_dir: Path) -> set[str]:
    """插件 pyproject.toml 声明的依赖归一名集合（无 pyproject 返回空集）。"""
    pp = plugin_dir / "pyproject.toml"
    if not pp.is_file():
        return set()
    try:
        import tomllib

        data = tomllib.load(open(pp, "rb"))
    except (OSError, tomllib.TOMLDecodeError):
        return set()
    deps = data.get("project", {}).get("dependencies", []) or []
    names: set[str] = set()
    for dep in deps:
        name = str(dep).split(";")[0].split("[")[0]
        for sep in ("=", "<", ">", "!", "~", " "):
            name = name.split(sep)[0]
        if name.strip():
            names.add(norm(name.strip()))
    return names


def installed_dist_names(host_venv: Path) -> set[str] | None:
    """包内 _host/.venv 实装发行包归一名全集；site-packages 缺失返回 None。"""
    candidates = [
        host_venv / "Lib" / "site-packages",
        *sorted(host_venv.glob("lib/python*/site-packages")),
    ]
    sp = next((p for p in candidates if p.is_dir()), None)
    if sp is None:
        return None
    import re

    dist_re = re.compile(r"^(.+)-(\d.*)\.dist-info$", re.IGNORECASE)
    return {norm(m.group(1)) for e in sp.iterdir() if (m := dist_re.match(e.name))}


# editable 本地源（SDK）不产生 dist-info，豁免依赖闭包比对
EDITABLE_DEPS = {"agentos-plugin-sdk"}


def check_host_venv_deps(shared: Path, host_venv: Path) -> list[Finding]:
    """断言合宿成员 pyproject 依赖 ⊆ 包内 _host/.venv 实装（判据 6）。"""
    findings: list[Finding] = []
    installed = installed_dist_names(host_venv)
    members: list[tuple[str, Path]] = []
    for pattern in PLUGIN_GLOBS:
        for plugin_dir in sorted(shared.glob(pattern)):
            group = manifest_host_group(plugin_dir)
            if group and (plugin_dir / "pyproject.toml").is_file():
                members.append((str(plugin_dir.relative_to(shared)), plugin_dir))
    if not members:
        return findings
    if installed is None:
        findings.append(
            Finding(
                "error",
                "HOST_VENV_SITE_PKG_MISSING",
                str(host_venv),
                "共享 venv 无 site-packages——依赖闭包无从校验，成员 import 必失败",
            )
        )
        return findings
    for rel, plugin_dir in members:
        missing = sorted(
            name
            for name in pyproject_dep_names(plugin_dir)
            if name not in installed and name not in EDITABLE_DEPS
        )
        if missing:
            findings.append(
                Finding(
                    "error",
                    "HOST_VENV_DEP_MISSING",
                    str(plugin_dir),
                    f"合宿成员 {rel} 声明的依赖 {missing} 未实装进包内共享 venv——"
                    "装机版调用期即 ModuleNotFoundError（web_operate 缺 httpx 实证）；"
                    "在 plugins/shared/_host 跑 uv sync 后重打包",
                )
            )
    return findings


def check_resources(root: Path) -> list[Finding]:
    findings: list[Finding] = []
    shared = root / SHARED_REL
    if not shared.is_dir():
        return [
            Finding(
                "error", "SHARED_DIR_MISSING", str(shared), "打包资源缺 plugins/shared"
            )
        ]

    # 1/2. 共享合宿基座 + 共享 venv 解释器
    host_dir = root / HOST_DIR_REL
    for name in HOST_BASE_FILES:
        if not (host_dir / name).is_file():
            findings.append(
                Finding(
                    "error",
                    "HOST_BASE_MISSING",
                    str(host_dir / name),
                    "合宿基座文件缺失——host.py 由宿主侧任务承载，缺它则 "
                    "light 合宿宿主无法 spawn（连带全部内置模式包）",
                )
            )
    host_venv = host_dir / ".venv"
    if venv_interpreter(host_venv) is None:
        findings.append(
            Finding(
                "error",
                "HOST_VENV_MISSING",
                str(host_venv),
                "共享 venv 解释器缺失（探测 .venv/Scripts/python.exe 与 "
                ".venv/bin/python 均不存在）——内核合宿 spawn fail-closed 只认它，"
                "全体 light 成员与内置模式包将 HOST_VENV_MISSING",
            )
        )
    else:
        # 6. 成员依赖闭包 ⊆ 包内共享 venv（解释器在位 ≠ 依赖完整）
        findings.extend(check_host_venv_deps(shared, host_venv))

    # 3/4/5. 定深扫描插件目录
    for pattern in PLUGIN_GLOBS:
        for plugin_dir in sorted(shared.glob(pattern)):
            if not (plugin_dir / "plugin.json").is_file():
                continue
            plugin_dir.relative_to(root)
            venv = plugin_dir / ".venv"
            if is_link(venv):
                findings.append(
                    Finding(
                        "error",
                        "JUNCTION_IN_PACKAGE",
                        str(venv),
                        "包内 .venv 为 junction/symlink——存绝对路径跨机必断，"
                        "打包产物不得携带（应由共享 venv 或装机侧重建承接）",
                    )
                )
                continue
            if venv.exists():
                group = manifest_host_group(plugin_dir)
                in_modes = plugin_dir.parent == (root / MODES_REL)
                if group or in_modes:
                    findings.append(
                        Finding(
                            "error",
                            "SLIM_MEMBER_VENV_PRESENT",
                            str(venv),
                            "合宿成员/内置模式包携带自身 .venv——运行期零消费"
                            "（进程从 _host 共享 venv 拉起），违反选型 c 瘦身结构",
                        )
                    )
                else:
                    findings.append(
                        Finding(
                            "info",
                            "INDEPENDENT_VENV_PRESENT",
                            str(venv),
                            "independent 类插件真实 .venv（合法：与共享环境同卷"
                            "硬链接或装机自愈重建所得）",
                        )
                    )
            # 残留半成品 venv（.venv-*）同口径检查
            for stale in sorted(plugin_dir.glob(".venv-*")):
                if stale.exists() and not is_link(stale):
                    findings.append(
                        Finding(
                            "info",
                            "STALE_VENV_DOTDIR",
                            str(stale),
                            "残留 .venv-* 目录（历史构建残留，建议清点）",
                        )
                    )
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("resources", type=Path, help="打包资源根（含 plugins/shared）")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args()

    root = args.resources.resolve()
    if not root.is_dir():
        print(f"资源根不存在：{root}", file=sys.stderr)
        return 1

    findings = check_resources(root)
    errors = [f for f in findings if f.level == "error"]
    if args.json:
        print(
            json.dumps(
                {
                    "root": str(root),
                    "ok": not errors,
                    "findings": [dataclasses.asdict(f) for f in findings],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print(f"资源根 {root}")
        if not findings:
            print("  结构合格：_host 共享 venv 在位，模式包/合宿成员已瘦身")
        for f in findings:
            mark = "ERROR" if f.level == "error" else "info "
            print(f"  [{mark}] {f.code}: {f.path}")
            print(f"          {f.detail}")
        print(
            f"\n结论：{'不合格（存在 error）' if errors else '合格'}"
            f"（error={len(errors)} info={len(findings) - len(errors)}）"
        )
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
