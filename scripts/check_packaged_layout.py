"""打包产物结构校验：A3 选型 c——内置模式包瘦身共享宿主 _host venv。

对一个打包资源根（electron-builder 产物 resources/、安装目录 resources/、或
任意同构 staging 目录）断言共享 venv 结构（BUG-66 裁决：R261 选型 c，
R208 先例——合宿成员跳过自身 venv、进程从 plugins/shared/_host/.venv 拉起）：

  1. 共享合宿基座在位：plugins/shared/_host/{host.py, pyproject.toml}；
  2. 共享 venv 解释器在位：_host/.venv/{Scripts/python.exe | bin/python}
     （内核合宿 spawn fail-closed 只认它——HOST_VENV_MISSING 即全体 light
     成员 + 内置模式包不可用）；
  3. 内置模式包（plugins/shared/mode_*，按 id 前缀识别）不带任何自身 .venv；
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

# 合宿基座必备文件（缺失 = 装机版合宿/mode pack 全体不可 spawn）
HOST_BASE_FILES = ("host.py", "pyproject.toml")

# 定深扫描布局（与 bootstrap_plugin_envs.py PLUGIN_GLOBS 同步；禁递归 glob）。
# 拍平布局（ADR 2026-10-13-plugin-flat-layout）：96 插件平铺至 plugins/shared/<id>，
# 一级 glob 即全集（_host/_libs 无 plugin.json 天然不入）。内置模式包不再有
# 专用 modes/ 桶，按 mode_ id 前缀识别（MODE_ID_PREFIX 同源规则）。
PLUGIN_GLOBS = [
    "*",
]


@dataclasses.dataclass
class Finding:
    level: str  # "error" | "warn" | "info"
    code: str
    path: str
    detail: str


def is_link(path: Path) -> bool:
    """junction 或 symlink（两者跨机/跨卷皆不可移植）。

    缺失/不可达路径返回 False（非 link）——包内插件允许不带 .venv（装机侧
    重建承接），Python 3.11 无 os.path.isjunction、走 lstat 分支时对缺失
    路径抛 FileNotFoundError（CI 2026-11-06 实证：3.14 本机不炸、3.11
    runner 炸）；断链 symlink 仍返回 True（is_symlink 不 follow 目标）。
    """
    try:
        if path.is_symlink():
            return True
        if hasattr(os.path, "isjunction"):
            return os.path.isjunction(path)
        st = path.lstat()
        return bool(st.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)
    except (FileNotFoundError, OSError):
        return False


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

        with open(pp, "rb") as fh:
            data = tomllib.load(fh)
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
            name for name in pyproject_dep_names(plugin_dir) if name not in installed and name not in EDITABLE_DEPS
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


# 随包解释器（非 venv，无 pyvenv.cfg）：editable 指向同样须落在资源根内
PREREQS_PYTHON_REL = Path("prereqs") / "python"
# editable 安装的路径载体（uv/pip 同款命名）
EDITABLE_PTH_GLOB = "__editable__*.pth"


def check_venv_home(root: Path, venv_dir: Path, *, will_relocate: bool = True) -> list[Finding]:
    """断言 venv 基座指针（pyvenv.cfg home）形态合法。

    背景（2026-11-12 实证，随包 venv 三形态全灭）：home 绝对=产物挪位后死链
    （打包位/worktree 绝对路径，第四犯同型）；home 相对=CWD 依赖（bash venv
    全量 exe 实测从异 cwd 启动即 ``No Python at``）；uv trampoline python.exe
    另烘创建机基座路径（cfg 修复也不生效）。结论：**随包 venv 无防挪位形态**，
    现役设计=resources 不带 venv（strip_packaged_venvs 出包剥离），装后由
    boot autoprovision 按包内 prereqs/python + wheelhouse 在安装位原地自建
    （基座指针天然正确）。装后位（will_relocate=False）包内绝对 home = 合法
    （autoprovision 原地生成形态）。
    """
    cfg = venv_dir / "pyvenv.cfg"
    if not cfg.is_file():
        return []  # 缺 cfg 的判据由调用方承载（host=HOST_VENV_CFG_MISSING；独立 venv 无 cfg=非 venv 形态）
    try:
        text = cfg.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return [Finding("error", "VENV_CFG_UNREADABLE", str(cfg), "pyvenv.cfg 不可读——venv 基座无从校验")]
    home = next(
        (value.strip() for line in text.splitlines() for name, sep, value in [line.partition("=")]
         if sep and name.strip().lower() == "home"),
        None,
    )
    if not home:
        return [Finding("error", "VENV_HOME_MISSING", str(cfg), "pyvenv.cfg 无 home 行——venv 解释器无法定位基座（非标准 venv）")]
    candidate = Path(home)
    if not candidate.is_absolute():
        return [Finding(
            "error",
            "VENV_HOME_RELATIVE_UNSUPPORTED",
            str(cfg),
            f"相对 home={home}——CWD 依赖（getpath 按 cwd 解析，异 cwd 启动即 "
            "``No Python at``，2026-11-12 实测）；venv 基座指针必须绝对且在包内，"
            "装后由 boot autoprovision 原地生成",
        )]
    if not is_within(candidate, root):
        return [Finding(
            "error",
            "VENV_HOME_OUT_OF_PACKAGE",
            str(cfg),
            f"venv 基座指向包外：{home}——净机无此路径，解释器找不到 stdlib（2026-11-12 "
            "装机实证：_host venv home 指向用户机 uv python）；随包 venv 属违规形态，"
            "由 strip_packaged_venvs 出包剥离、装后 autoprovision 自建",
        )]
    if will_relocate:
        return [Finding(
            "warn",
            "VENV_HOME_ABSOLUTE_IN_PACKAGE",
            str(cfg),
            f"随包 venv 基座为打包位绝对路径：{home}——产物挪位后即死链，且 uv "
            "trampoline 可能另烘创建机基座路径（cfg 修复也不生效）；随包 venv 属"
            "违规形态，应由 strip_packaged_venvs 出包剥离、装后 boot autoprovision "
            "在安装位原地自建（HOST_VENV_MISSING 载荷阶段已豁免）",
        )]
    return []


def site_packages_dirs(venv_dir: Path) -> list[Path]:
    """venv/解释器目录下的 site-packages 全集（Windows Lib/ 与 POSIX lib/pythonX.Y/）。"""
    candidates = [venv_dir / "Lib" / "site-packages", *sorted(venv_dir.glob("lib/python*/site-packages"))]
    return [candidate for candidate in candidates if candidate.is_dir()]


def is_within(path: Path, root: Path) -> bool:
    """path 是否落在 root 之内（解析后按公共前缀判；非法路径按越界处理）。"""
    try:
        resolved_path = path.resolve()
        resolved_root = root.resolve()
        return resolved_path == resolved_root or resolved_root in resolved_path.parents
    except OSError:  # 非法/不可解析路径：按越界处理（fail-closed）
        return False


def check_editable_targets(root: Path, site_packages: Path, *, will_relocate: bool = True) -> list[Finding]:
    """断言 editable 安装可随产物挪位解析（不可挪位 = 裸机必炸的部署缺陷）。

    背景（2026-10-29 第三犯 + 2026-11-10 复核 + 2026-11-12 第四犯）：仓内 venv
    被整目录照搬进包，``__editable__*.pth`` 里写的是**绝对路径**——开发机路径，
    或**打包位/worktree 绝对路径**（第四犯：打包机上原地存在所以原地校验全绿，
    NSIS 装机挪位/打包树清理后即死链）。用户机 import agentos_plugin_sdk 直接
    失败——全部 sidecar 插件起不来。

    合法形态：相对 site-packages 的相对路径行（site.addpackage 按 .pth 所在
    目录解析，随树挪位成立）或 import 行。绝对路径行仅在装后位校验
    （``will_relocate=False``，树不再挪动、路径由装机侧原地生成）合法。
    """
    findings: list[Finding] = []
    for pth in sorted(site_packages.glob(EDITABLE_PTH_GLOB)):
        try:
            lines = pth.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for raw in lines:
            line = raw.strip()
            if not line or line.startswith("#") or line.startswith("import "):
                continue  # 空行/注释/可执行行不是路径行
            candidate = Path(line)
            if candidate.is_absolute():
                if not is_within(candidate, root):
                    findings.append(
                        Finding(
                            "error",
                            "EDITABLE_TARGET_OUT_OF_ROOT",
                            str(pth),
                            f"editable 安装指向打包资源根之外：{line}——用户机无此路径，"
                            "该发行包 import 必失败（SDK 尤甚：全部 sidecar 插件连坐）；"
                            "打包前把 .pth 重定位到包内 plugins/sdk/src（或改非 editable 安装）",
                        )
                    )
                    break
                if will_relocate:
                    findings.append(
                        Finding(
                            "error",
                            "EDITABLE_ABSOLUTE_IN_PACKAGE",
                            str(pth),
                            f"editable 安装写死打包位绝对路径：{line}——产物安装挪位/"
                            "打包树清理后即死链（2026-11-12 第四犯：_host venv 指向打包 "
                            "worktree，删树后全部合宿宿主 spawn 即 ModuleNotFoundError，"
                            "crash loop）；出包前由 relocate_packaged_editable.py 改写为"
                            "相对 site-packages 的相对路径行",
                        )
                    )
                    break
                continue
            # 相对路径行（重定位后形态）：按 site-packages 解析后必须落在包内且真实存在
            resolved = (site_packages / candidate).resolve()
            if not is_within(resolved, root):
                findings.append(
                    Finding(
                        "error",
                        "EDITABLE_TARGET_OUT_OF_ROOT",
                        str(pth),
                        f"相对路径行解析越界：{line} -> {resolved}——逃出资源根，"
                        "用户机 import 必失败",
                    )
                )
                break
            if not resolved.exists():
                findings.append(
                    Finding(
                        "error",
                        "EDITABLE_TARGET_MISSING",
                        str(pth),
                        f"相对路径行解析目标不存在：{line} -> {resolved}——运行期该行被"
                        " site 静默跳过，SDK import 必失败",
                    )
                )
                break
    return findings


def check_resources(root: Path, *, will_relocate: bool = True) -> list[Finding]:
    """检查打包资源根。will_relocate=True（默认，打包位）拦绝对路径行；False
    （装后位 --installed）只拦越界，包内绝对路径合法（树不再挪动）。"""
    findings: list[Finding] = []
    shared = root / SHARED_REL
    if not shared.is_dir():
        return [Finding("error", "SHARED_DIR_MISSING", str(shared), "打包资源缺 plugins/shared")]

    # 1/2. 共享合宿基座 + 共享 venv 解释器
    host_dir = root / HOST_DIR_REL
    for name in HOST_BASE_FILES:
        if not (host_dir / name).is_file():
            findings.append(
                Finding(
                    "error",
                    "HOST_BASE_MISSING",
                    str(host_dir / name),
                    "合宿基座文件缺失——host.py 由宿主侧任务承载，缺它则 light 合宿宿主无法 spawn（连带全部内置模式包）",
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
        # 2b. venv 身份文件在位（解释器文件 ≠ 可用 venv）
        if not (host_venv / "pyvenv.cfg").is_file():
            findings.append(
                Finding(
                    "error",
                    "HOST_VENV_CFG_MISSING",
                    str(host_venv / "pyvenv.cfg"),
                    "共享 venv 缺 pyvenv.cfg——venv 的 python.exe 启动器据此解析基座，"
                    "缺失即静默退回外部 Python，venv 内 site-packages 全部失效"
                    "（2026-11-10 装机实证：aiohttp 命中外部命名空间包 → sync_agent "
                    "上行永久失败 → outbox 触顶 → cohost 心跳停滞自杀循环）；"
                    "重建：uv sync --project plugins/shared/_host",
                )
            )
        # 2c. editable 安装可随产物挪位解析（打包位绝对路径/越界 = 裸机必炸）
        for site_packages in site_packages_dirs(host_venv):
            findings.extend(check_editable_targets(root, site_packages, will_relocate=will_relocate))
        # 2c'. venv 基座指针（pyvenv.cfg home）同口径：净机无开发机基座即死
        findings.extend(check_venv_home(root, host_venv, will_relocate=will_relocate))
        # 6. 成员依赖闭包 ⊆ 包内共享 venv（解释器在位 ≠ 依赖完整）
        findings.extend(check_host_venv_deps(shared, host_venv))

    # 2d. 随包解释器的 editable 指向（无 venv 身份，只有越界/挪位判据）
    for site_packages in site_packages_dirs(root / PREREQS_PYTHON_REL):
        findings.extend(check_editable_targets(root, site_packages, will_relocate=will_relocate))

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
                is_mode_pkg = plugin_dir.name.startswith("mode_")
                if group or is_mode_pkg:
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
                            "independent 类插件真实 .venv（合法：与共享环境同卷硬链接或装机自愈重建所得）",
                        )
                    )
                    # 独立 venv 基座指针同口径校验（合宿成员/模式包自带 venv 本身即
                    # error，不重复报 home 面）
                    findings.extend(check_venv_home(root, venv, will_relocate=will_relocate))
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
    # Windows 控制台默认 cp1252，输出含 CJK 时 print 直接 UnicodeEncodeError
    # （2026-11-06 冒烟步经 subprocess 复用本脚本实证——gate 封装入口的
    # reconfigure 不覆盖本 CLI 入口，同族第五犯）——入口重配置 UTF-8。
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("resources", type=Path, help="打包资源根（含 plugins/shared）")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument(
        "--installed",
        action="store_true",
        help="装后位校验（树不再挪位：只拦越界，包内绝对路径合法）",
    )
    args = parser.parse_args()

    root = args.resources.resolve()
    if not root.is_dir():
        print(f"资源根不存在：{root}", file=sys.stderr)
        return 1

    findings = check_resources(root, will_relocate=not args.installed)
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
            mark = {"error": "ERROR", "warn": "WARN "}.get(f.level, "info ")
            print(f"  [{mark}] {f.code}: {f.path}")
            print(f"          {f.detail}")
        warn_count = sum(1 for f in findings if f.level == "warn")
        print(
            f"\n结论：{'不合格（存在 error）' if errors else '合格'}"
            f"（error={len(errors)} warn={warn_count} info={len(findings) - len(errors) - warn_count}）"
        )
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
