"""插件 venv 引导器：消除重复 venv（审计 / 应用两态）。

机制事实（勿凭感觉改）：
- 合宿 light 成员运行在 plugins/shared/_host/.venv 宿主进程内（host.py 经 sys.path
  装载成员代码），成员自身 .venv 运行期零消费——纯属冗余。
- 独立 sidecar 插件由内核 invoker fail-closed 要求「插件目录内有 .venv 解释器」
  （kernel/crates/invoker resolve_sidecar_command）；其 uv.lock 解析结果与共享环境
  _host/.venv 实装版本逐包相等的插件，venv 内容与共享环境同源，可用目录
  junction/symlink 指向共享环境替代——invoker 经 junction 命中解释器，零内核改动。
- 其余（依赖超出共享环境的重插件、双 venv 特殊栈）保持独立，不做任何事。

分类全部机械推导（manifest host_group + uv.lock 逐包版本比对），无指纹推理——
依赖变化后重跑本脚本即重分类；junction 不再合格会被摘除并提示回归独立 venv。

用法：
  python scripts/bootstrap_plugin_envs.py            # 审计（默认，只读）
  python scripts/bootstrap_plugin_envs.py --apply    # 应用（删冗余 / 建 junction）
  python scripts/bootstrap_plugin_envs.py --json     # 机器可读输出（打包管线消费）
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import re
import shutil
import stat
import sys
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SHARED_ENV = REPO / "plugins" / "shared" / "_host" / ".venv"
SDK_DIST_NAME = "agentos-plugin-sdk"

# 定深扫描（禁递归 glob：dsh_adapter 存在自嵌套 node_modules junction 会挂死）
PLUGIN_GLOBS = [
    "plugins/shared/system/*",
    "plugins/shared/tools/*",
    "plugins/shared/pipeline/core/*",
    "plugins/shared/pipeline/input/*",
    "plugins/shared/pipeline/output/*",
    "plugins/shared/modes/*",
    "plugins/shared/db_admin",
    "plugins/shared/metrics_admin",
    "plugins/shared/user_admin",
]
# 永不触碰
PROTECTED = {
    REPO / ".venv",
    REPO / "plugins" / "sdk" / ".venv",
    SHARED_ENV,
}

_DIST_INFO_RE = re.compile(r"^(?P<name>.+)-(?P<ver>\d.*)\.dist-info$", re.IGNORECASE)


def norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def is_junction(path: Path) -> bool:
    if hasattr(os.path, "isjunction"):
        return os.path.isjunction(path)
    st = path.lstat()
    return bool(st.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT) and (
        getattr(st, "st_reparse_tag", 0) == stat.IO_REPARSE_TAG_MOUNT_POINT
    )


def shared_env_packages() -> dict[str, str]:
    """共享环境实装发行包：归一名 -> 版本（site-packages/*.dist-info 扫描）。"""
    candidates: list[Path] = [
        SHARED_ENV / "Lib" / "site-packages",
        *sorted(SHARED_ENV.glob("lib/python*/site-packages")),
    ]
    sp = next(p for p in candidates if p.exists())
    pkgs: dict[str, str] = {}
    for entry in sp.iterdir():
        m = _DIST_INFO_RE.match(entry.name)
        if m:
            pkgs[norm(m.group("name"))] = m.group("ver")
    return pkgs


def lock_packages(plugin_dir: Path) -> dict[str, str] | None:
    """插件 uv.lock 的 registry 解析结果：归一名 -> 版本。

    跳过 virtual（项目根）与 editable（本地 SDK 源）；git 等非常规源按不满足处理。
    无 uv.lock 返回 None（fail-closed，不可 junction）。
    """
    lock = plugin_dir / "uv.lock"
    if not lock.exists():
        return None
    with open(lock, "rb") as f:
        data = tomllib.load(f)
    pkgs: dict[str, str] = {}
    for pkg in data.get("package", []):
        source = pkg.get("source") or {}
        if "registry" not in source:
            continue  # virtual 根 / editable SDK / git 等不参与比对
        pkgs[norm(pkg["name"])] = pkg["version"]
    return pkgs


def pyproject_dep_names(plugin_dir: Path) -> set[str]:
    pp = plugin_dir / "pyproject.toml"
    if not pp.exists():
        return set()
    with open(pp, "rb") as f:
        data = tomllib.load(f)
    deps = data.get("project", {}).get("dependencies", []) or []
    names = set()
    for dep in deps:
        name = str(dep).split(";")[0].split("[")[0]
        for sep in ("=", "<", ">", "!", "~", " "):
            name = name.split(sep)[0]
        names.add(norm(name.strip()))
    return names


def venv_state(plugin_dir: Path) -> str:
    venv = plugin_dir / ".venv"
    if not venv.exists():
        return "absent"
    if is_junction(venv) or venv.is_symlink():
        return "junction" if venv.resolve() == SHARED_ENV.resolve() else "foreign-link"
    return "real"


def dir_size_mb(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                pass
    return total // (1024 * 1024)


def classify(plugin_dir: Path, shared: dict[str, str]) -> str:
    """类别：light（合宿成员，venv 冗余）/ linkable（锁与共享环境逐包相等）/ independent。

    合宿成员判定与内核 is_cohost_member 同源对齐：声明**任意** host_group
    （light / light_stable / …多组扩展）即成员——运行期都从 _host 共享 venv
    拉起，自身 .venv 零消费。只认 "light" 会把 light_stable 成员误判
    independent（漏删 + launcher 回潮重建）。
    """
    manifest = plugin_dir / "plugin.json"
    try:
        meta = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "independent"
    if meta.get("host_group"):
        return "light"
    locked = lock_packages(plugin_dir)
    if locked and all(shared.get(name) == ver for name, ver in locked.items()):
        return "linkable"
    return "independent"


def make_link(plugin_dir: Path, actions: list[str], warnings: list[str]) -> None:
    link = plugin_dir / ".venv"
    if link.exists() or link.is_symlink():
        if is_junction(link) or link.is_symlink():
            actions.append(f"skip(junction已就绪) {plugin_dir.name}")
            return
        shutil.rmtree(link)
    if sys.platform == "win32":
        import _winapi

        _winapi.CreateJunction(str(SHARED_ENV), str(link))
    else:
        link.symlink_to(SHARED_ENV, target_is_directory=True)
    exe_dir = "Scripts" if sys.platform == "win32" else "bin"
    if not (link / exe_dir).exists():
        warnings.append(f"junction 后解释器缺失: {link}")
    actions.append(f"junction→{SHARED_ENV.name} {plugin_dir.name}")


@dataclasses.dataclass
class Row:
    dir: str
    cls: str
    venv: str
    size_mb: int
    over_deps: list[str]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="执行变更（默认只审计）")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args()

    if not SHARED_ENV.exists():
        print(f"共享环境缺失：{SHARED_ENV}（先 uv sync --project plugins/shared/_host）", file=sys.stderr)
        return 1
    shared = shared_env_packages()

    rows: list[Row] = []
    for pattern in PLUGIN_GLOBS:
        for plugin_dir in sorted(REPO.glob(pattern)):
            if not (plugin_dir / "plugin.json").exists():
                continue
            cls = classify(plugin_dir, shared)
            over_deps = (
                sorted(pyproject_dep_names(plugin_dir) - set(shared))
                if cls == "light"
                else []
            )
            venv_path = plugin_dir / ".venv"
            rows.append(
                Row(
                    dir=str(plugin_dir.relative_to(REPO)),
                    cls=cls,
                    venv=venv_state(plugin_dir),
                    size_mb=dir_size_mb(venv_path) if venv_path.exists() else 0,
                    over_deps=over_deps,
                )
            )

    actions: list[str] = []
    warnings: list[str] = []
    if args.apply:
        for row in rows:
            plugin_dir = REPO / row.dir
            venv = plugin_dir / ".venv"
            if venv in PROTECTED or row.venv in ("absent", "junction"):
                continue
            if row.cls == "light" and row.venv == "real":
                try:
                    shutil.rmtree(venv)
                    actions.append(f"删除冗余venv {row.dir} (-{row.size_mb}MB)")
                except OSError as exc:
                    warnings.append(f"删除失败(文件占用?): {row.dir}: {exc}")
            elif row.venv == "foreign-link":
                venv.unlink()
                warnings.append(f"异源链接摘除(请人工核对指向): {row.dir}")
            elif row.cls == "linkable" and row.venv == "real":
                make_link(plugin_dir, actions, warnings)
            elif row.cls == "independent" and row.venv == "junction":
                venv.unlink()
                warnings.append(
                    f"依赖已超出共享环境，junction摘除，请回独立环境: uv sync --project {row.dir}"
                )

    freed = sum(
        r.size_mb for r in rows if r.cls in ("light", "linkable") and r.venv == "real"
    )
    if args.json:
        print(json.dumps({"rows": [dataclasses.asdict(r) for r in rows], "actions": actions, "warnings": warnings, "freed_mb": freed}, ensure_ascii=False, indent=2))
    else:
        by_class: dict[str, int] = {}
        for r in rows:
            by_class[r.cls] = by_class.get(r.cls, 0) + 1
        print(f"插件总数 {len(rows)}  分类 {by_class}  共享环境实装 {len(shared)} 包")
        for r in rows:
            flag = f"  ⚠ 超依赖{r.over_deps}(light成员运行时用共享env,请准入复核)" if r.over_deps else ""
            print(f"  [{r.cls:>11}] venv={r.venv:<9} {r.size_mb:>4}MB  {r.dir}{flag}")
        print(f"\n可释放(名义) {freed}MB")
        if actions:
            print("已执行:")
            for a in actions:
                print(f"  {a}")
        if warnings:
            print("警告:", file=sys.stderr)
            for w in warnings:
                print(f"  {w}", file=sys.stderr)
        if not args.apply:
            print("\n(审计模式，未做任何变更；--apply 执行)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
