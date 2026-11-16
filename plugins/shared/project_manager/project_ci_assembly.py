"""CI 包出生组装（设计真值 docs/working/项目类型CI包设计_20261030.md §4.1/§5/§6.2）。

project.create 出生初始化的 ci 段：按四路信号（类型配方默认/detect_file/语言清单/
语言代码文件）圈定 pack 集合 → 拷 pack 物料到 <项目>/.agentos/ci/（逐文件、已存在
不覆盖、版本闸严格大于才刷新）→ 组装 gates.json（跨 pack 同 id 整体报错）→ 写
.git/hooks/pre-commit（已有同名不覆盖 + warn）→ 出生报告（启用/跳过的面+判定原因）。

`local/` 扩展面豁免于版本闸：本模块永不写 local/（agent 与用户沉淀的资产零风险）。
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

# 仓库根（project_ci_assembly.py → project_manager/ → shared/ → plugins/ → 根）
_REPO_ROOT = Path(__file__).resolve().parents[3]
PACKS_ROOT = _REPO_ROOT / "packs" / "ci"

# 语言清单文件 → 语言编程 pack（§4.1 信号 3；语言 pack 未出厂时记入出生报告）
_MANIFEST_SIGNALS: dict[str, str] = {
    "pyproject.toml": "python",
    "Cargo.toml": "rust",
    "package.json": "ts",
    "go.mod": "go",
}
# 语言代码文件 glob → pack（§4.1 信号 4；gdscript 归 godot pack 编程面）
_CODE_SIGNALS: dict[str, str] = {
    "*.py": "python",
    "*.gd": "godot",
    "*.ts": "ts",
    "*.tsx": "ts",
    "*.rs": "rust",
    "*.go": "go",
}
# 信号扫描剪枝：噪声目录不入扫描面
_PRUNE_DIRS = {
    ".git",
    ".agentos",
    "node_modules",
    "__pycache__",
    ".godot",
    ".venv",
    "venv",
    ".pytest_cache",
    ".mypy_cache",
}
_CODE_SCAN_CAP = 20000  # 防御上限：超大规模目录树的扫描边界


def _version_key(version: str) -> tuple[int, ...]:
    """'1.2.3' → (1, 2, 3)；非数字段按 0 处理（比较恒可进行）。"""
    parts: list[int] = []
    for seg in str(version).split("."):
        try:
            parts.append(int(seg))
        except ValueError:
            parts.append(0)
    return tuple(parts) or (0,)


def _probe_godot_bin() -> str:
    """godot 二进制探测（§3.3 探测序 env GODOT_BIN → PATH）；未命中返回空串。"""
    env_bin = os.environ.get("GODOT_BIN", "").strip()
    if env_bin:
        return env_bin
    return _which("godot")


def _which(tool: str) -> str:
    import shutil

    return shutil.which(tool) or ""


def _iter_project_files(project: Path) -> list[str]:
    """项目内文件清单（posix 相对路径），剪枝噪声目录，防御性上限封顶。"""
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(project):
        dirnames[:] = [d for d in dirnames if d not in _PRUNE_DIRS]
        for name in filenames:
            rel = Path(dirpath, name).relative_to(project).as_posix()
            found.append(rel)
            if len(found) >= _CODE_SCAN_CAP:
                return found
    return found


def detect_ci_packs(
    project_path: str,
    routed_recipe: dict[str, Any] | None,
    recipes: dict[str, dict[str, Any]],
    available: set[str],
) -> tuple[list[str], list[str]]:
    """四路信号圈定 pack 集合（§4.1）：返回（可用 pack 有序集, 跳过原因清单）。

    顺序即装配序：底座 base → 类型配方默认 → detect_file 命中类型 → 语言清单 →
    代码文件信号；同名先到先得（去重保序）。
    """
    candidates: dict[str, str] = {}

    def add(name: str, why: str) -> None:
        candidates.setdefault(str(name), why)

    add("base", "底座默认")
    for pack in (routed_recipe or {}).get("ci_packs") or []:
        add(str(pack), "类型配方默认")
    project = Path(project_path)
    for ptype, recipe in recipes.items():
        detect = str(recipe.get("detect_file") or "")
        if detect and (project / detect).is_file():
            for pack in recipe.get("ci_packs") or []:
                add(str(pack), f"detect_file 命中类型 {ptype}")
    for manifest, pack in _MANIFEST_SIGNALS.items():
        if (project / manifest).is_file():
            add(pack, f"语言清单 {manifest}")
    if Path(project_path).is_dir():
        hit: dict[str, str] = {}
        for rel in _iter_project_files(project):
            for pattern, pack in _CODE_SIGNALS.items():
                if pack not in hit and rel.endswith(pattern[1:]):
                    hit[pack] = f"代码文件 {pattern}"
        for pack, why in hit.items():
            add(pack, why)
    skipped = [
        f"{name}（信号 {why}）：未出厂" for name, why in candidates.items() if name not in available
    ]
    return [name for name in candidates if name in available], skipped


def _load_pack(pack_dir: Path, name: str) -> tuple[str, list[dict[str, Any]]]:
    """读 pack.json + gates.json（缺一即 FileNotFoundError，fail-closed 同 addons 原语）。"""
    manifest_path = pack_dir / "pack.json"
    gates_path = pack_dir / "gates.json"
    if not manifest_path.is_file() or not gates_path.is_file():
        raise FileNotFoundError(f"CI 包缺失或物料不全: {pack_dir}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if str(manifest.get("name") or "") != name:
        raise ValueError(f"CI 包 pack.json name 与目录不符: {pack_dir}")
    data = json.loads(gates_path.read_text(encoding="utf-8"))
    gates = data.get("gates") if isinstance(data, dict) else data
    if not isinstance(gates, list):
        raise ValueError(f"CI 包 gates.json 须为数组或含 gates 键: {gates_path}")
    return str(manifest.get("version") or "0.0.0"), gates  # type: ignore[return-value]


def _merge_gates(
    packs: list[str], packs_root: Path, recipe: dict[str, Any]
) -> tuple[dict[str, str], list[dict[str, Any]], str]:
    """按装配序合并各 pack 门禁 + 烘焙出生探测（§3.3）；同 id 整体报错（§6.2）。"""
    versions: dict[str, str] = {}
    merged: list[dict[str, Any]] = []
    seen: dict[str, str] = {}
    for name in packs:
        version, gates = _load_pack(packs_root / name, name)
        versions[name] = version
        for gate in gates:
            gate_id = str(gate.get("id") or "")
            if gate_id in seen:
                raise ValueError(f"CI 门禁 id 冲突: {gate_id}（{seen[gate_id]} × {name}）")
            seen[gate_id] = name
            merged.append(gate)
    probe = ""
    for gate in merged:
        if gate.get("id") == "addons_present":
            addons = [str(a) for a in recipe.get("addons") or []]
            if addons:
                gate["run"] = [*gate["run"], "--addons", ",".join(addons)]
        if gate.get("id") == "godot_headless_import":
            probe = _probe_godot_bin()
            if probe:
                gate["run"] = [*gate["run"], "--godot", probe]
    return versions, merged, probe


def _copy_pack_materials(pack_dir: Path, ci_dir: Path, overwrite: bool) -> None:
    """拷一个 pack 的物料进项目（逐文件、已存在不覆盖；overwrite=版本闸刷新）。

    消费件不拷：pack.json/gates.json（进组装产物）；hooks/ 走钩子装载（调用方处理）。
    运行残留不拷：__pycache__/*.pyc/last_run.json——pack 真源被本地执行污染的产物
    入库即被项目产物闸自己拦截（CI 框架残留不得脏用户提交）。
    """
    for src in sorted(pack_dir.rglob("*")):
        if not src.is_file():
            continue
        rel = src.relative_to(pack_dir)
        if rel.parts[0] in ("pack.json", "gates.json", "hooks"):
            continue
        if "__pycache__" in rel.parts or src.suffix == ".pyc" or src.name == "last_run.json":
            continue
        dst = ci_dir / rel
        if dst.exists() and not overwrite:
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)


def apply_ci_packs(
    project_path: str, recipe: dict[str, Any], all_recipes: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    """执行 CI 包出生组装；返回出生报告（异常整体上抛，调用方 fail-closed）。"""
    project = Path(project_path)
    ci_dir = project / ".agentos" / "ci"
    packs, skipped = detect_ci_packs(
        project_path, recipe, all_recipes, {p.name for p in PACKS_ROOT.iterdir()} if PACKS_ROOT.is_dir() else set()
    )
    warnings: list[str] = list(skipped)
    if "base" not in packs:
        raise ValueError("CI 包装配缺底座 base（runner 与钩子由 base 携带，不可省）")

    versions, gates, godot_probe = _merge_gates(packs, PACKS_ROOT, recipe)

    # 版本闸（§5）：已装版本严格大于等于出厂 → 工厂面跳过刷新；local/ 永不触碰
    installed_versions: dict[str, str] = {}
    if (ci_dir / "gates.json").is_file():
        try:
            prev = json.loads((ci_dir / "gates.json").read_text(encoding="utf-8"))
            installed_versions = {
                str(k): str(v) for k, v in (prev.get("packs") or {}).items()
            } if isinstance(prev, dict) else {}
        except (json.JSONDecodeError, OSError):
            installed_versions = {}  # 组装产物损坏 → 视为未装，按新装修复
    packs_skipped: list[str] = list(skipped)  # 未出厂候选 + 版本闸跳过，同列报告
    installed_now: list[str] = []
    hook_dst = project / ".git" / "hooks" / "pre-commit"
    for name in packs:
        version = versions[name]
        prev = installed_versions.get(name)
        if prev is not None and _version_key(version) <= _version_key(prev):
            packs_skipped.append(f"{name}：版本闸跳过（已装 {prev} ≥ 出厂 {version}）")
            continue
        _copy_pack_materials(PACKS_ROOT / name, ci_dir, overwrite=prev is not None)
        installed_now.append(name)

    # 钩子装载（§5）：.git/hooks/pre-commit 直写，已有同名不覆盖 + warn（用户资产优先）
    hook_installed = False
    hook_skipped_reason = ""
    hook_src = PACKS_ROOT / "base" / "hooks" / "pre-commit"
    if hook_dst.exists():
        hook_skipped_reason = f"已有同名钩子不覆盖（用户资产优先）: {hook_dst}"
        warnings.append(hook_skipped_reason)
    elif hook_src.is_file():
        hook_dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(hook_src, hook_dst)
        os.chmod(hook_dst, 0o755)
        hook_installed = True
    else:
        hook_skipped_reason = f"钩子源缺失: {hook_src}"
        warnings.append(hook_skipped_reason)

    # 组装 gates.json（工厂面，版本闸管辖）+ local 扩展面目录（本模块永不写其内容）
    ci_dir.mkdir(parents=True, exist_ok=True)
    (ci_dir / "local").mkdir(exist_ok=True)
    (ci_dir / "gates.json").write_text(
        json.dumps({"version": 1, "packs": versions, "gates": gates}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return {
        "installed": True,
        "packs": versions,
        "packs_installed": installed_now,
        "packs_skipped": packs_skipped,
        "gates_total": len(gates),
        "hook_installed": hook_installed,
        "hook_skipped_reason": hook_skipped_reason,
        "probes": {"godot_bin": godot_probe},
        "warnings": warnings,
    }
