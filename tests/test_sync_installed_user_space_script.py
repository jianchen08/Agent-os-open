# @feature: FP-0.2.一 双根插件供给同步脚本 | @ci: python-coverage
"""sync_installed_user_space 脚本契约测试。

锁四个硬行为：镜像语义（复制+删源侧已消失+data/ 保留+排除名单）、venv 供给
（源 .venv 整树复制）、config 整覆盖、以及两条拒绝边界（无 --user-root 拒写 /
目标落装机包安装目录内拒写）。uv 现建分支依赖外部 uv，不进单测面。

加载经 importlib 唯名模块（scripts/ 非包，防裸名串扰）。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "sync_installed_user_space.py"


@pytest.fixture
def mod() -> Any:
    spec = importlib.util.spec_from_file_location("sync_installed_user_space_test_mod", _SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    m = importlib.util.module_from_spec(spec)
    sys.modules["sync_installed_user_space_test_mod"] = m
    spec.loader.exec_module(m)
    return m


def _make_plugin(root: Path, name: str = "demo_plugin") -> Path:
    src = root / "src_plugin" / name
    (src / "sub").mkdir(parents=True)
    (src / ".venv" / "Scripts").mkdir(parents=True)
    (src / "__pycache__").mkdir(parents=True)
    (src / "plugin.json").write_text('{"id": "demo"}', encoding="utf-8")
    (src / "server.py").write_text("print('hi')\n", encoding="utf-8")
    (src / "sub" / "helper.py").write_text("X = 1\n", encoding="utf-8")
    (src / ".venv" / "Scripts" / "python.exe").write_text("venv-marker", encoding="utf-8")
    (src / "__pycache__" / "junk.pyc").write_text("junk", encoding="utf-8")
    return src


def test_mirror_copies_and_excludes(tmp_path: Path, mod: Any) -> None:
    """镜像复制源文件；.venv/__pycache__ 不进镜像（venv 单独供给）。"""
    src = _make_plugin(tmp_path)
    dst = tmp_path / "user_root" / "plugins" / "demo_plugin"
    actions = mod._mirror_tree(src, dst)

    copied = {rel for act, rel in actions if act == "copy"}
    assert "plugin.json" in copied
    assert str(Path("sub/helper.py").as_posix()) in copied
    assert dst.joinpath("plugin.json").exists()
    assert not dst.joinpath(".venv").exists(), "venv 不在镜像内，由 _provision_venv 单独供给"
    assert not dst.joinpath("__pycache__").exists()


def test_mirror_deletes_stale_but_preserves_data(tmp_path: Path, mod: Any) -> None:
    """源侧已消失的文件被删（改名类更新必须）；data/ 用户数据只增不删。"""
    src = _make_plugin(tmp_path)
    dst = tmp_path / "user_root" / "plugins" / "demo_plugin"
    mod._mirror_tree(src, dst)

    (dst / "obsolete.py").write_text("old", encoding="utf-8")
    (dst / "sub" / "gone.py").write_text("old", encoding="utf-8")
    (dst / "data").mkdir(parents=True, exist_ok=True)
    (dst / "data" / "user_state.json").write_text("{}", encoding="utf-8")
    actions = mod._mirror_tree(src, dst)
    deleted = {rel for act, rel in actions if act == "delete"}

    assert "obsolete.py" in deleted, "源侧已消失的文件必须删（镜像语义）"
    assert not (dst / "obsolete.py").exists()
    assert not (dst / "sub" / "gone.py").exists()
    assert (dst / "data" / "user_state.json").exists(), "data/ 用户数据只增不删"
    assert not any(str(Path("data")).replace("\\", "/") in rel for act, rel in actions if act == "delete")


def test_provision_venv_copies_from_source(tmp_path: Path, mod: Any) -> None:
    """源侧有 .venv → 整树复制到目标（同机 editable SDK 路径有效）。"""
    src = _make_plugin(tmp_path)
    dst = tmp_path / "user_root" / "plugins" / "demo_plugin"
    mod._mirror_tree(src, dst)

    note = mod._provision_venv(src, dst, dry_run=False)
    assert "复制" in note
    assert dst.joinpath(".venv", "Scripts", "python.exe").read_text(encoding="utf-8") == "venv-marker"

    again = mod._provision_venv(src, dst, dry_run=False)
    assert "跳过" in again, "已存在不重建（幂等）"


def test_config_sync_overwrites_single_file(tmp_path: Path, mod: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """config 单文件整覆盖到 <user_root>/config/<同相对路径>。"""
    monkeypatch.setattr(mod, "_REPO_ROOT", tmp_path / "repo")
    repo_cfg = tmp_path / "repo" / "config" / "pipelines"
    repo_cfg.mkdir(parents=True)
    (repo_cfg / "autonomous.yaml").write_text("name: new\n", encoding="utf-8")

    user_root = tmp_path / "user_root"
    mod.sync_config("pipelines/autonomous.yaml", user_root, dry_run=False)

    out = user_root / "config" / "pipelines" / "autonomous.yaml"
    assert out.read_text(encoding="utf-8") == "name: new\n"


def test_resolve_user_root_refuses_missing(tmp_path: Path, mod: Any) -> None:
    """无 --user-root 拒绝执行（静默写装机版用户空间违反零共享裁定）。"""
    with pytest.raises(SystemExit, match="写目标必须显式"):
        mod.resolve_user_root(None)


def test_resolve_user_root_refuses_install_dir(tmp_path: Path, mod: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """目标落在装机包安装目录子树内 → 拒绝（不动装机包硬边界）。"""
    fake_install = tmp_path / "Programs" / "agent-os"
    fake_install.mkdir(parents=True)
    monkeypatch.setattr(mod, "_existing_install_dirs", lambda: [fake_install])

    with pytest.raises(SystemExit, match="不动装机包"):
        mod.resolve_user_root(str(fake_install / "somewhere" / "user"))

    # 安装目录之外合法通过
    ok = mod.resolve_user_root(str(tmp_path / "real_user_root"))
    assert ok == (tmp_path / "real_user_root").resolve()


# ── Tier2 覆盖规则四态（ADR 2026-11-07 决策 2.1；双源版本闸已退役）──


def _fake_repo(tmp_path: Path, tier1_ids: list[str] | None = None) -> Path:
    """假仓根：config/kernel/plugin_tiers.yaml（可指定 Tier1 id）+ 空布局。"""
    repo = tmp_path / "repo"
    cfg = repo / "config" / "kernel"
    cfg.mkdir(parents=True, exist_ok=True)
    tier1_list = "\n".join(f"    - {i}" for i in (tier1_ids or []))
    (cfg / "plugin_tiers.yaml").write_text(
        "tier1:\n  safety_plugins:\n" + tier1_list + "\n  shared_modules: []\n",
        encoding="utf-8",
    )
    return repo


def _repo_plugin(
    repo: Path, name: str = "demo_plugin", version: str = "1.0.0", body: str = "print('hi')\n"
) -> Path:
    """假仓内插件目录（带 .venv 桩走整树复制分支，不触发 uv 子进程）。"""
    src = repo / "plugins" / "shared" / name
    src.mkdir(parents=True, exist_ok=True)
    (src / "plugin.json").write_text(
        json.dumps({"id": "demo", "version": version}), encoding="utf-8"
    )
    (src / "server.py").write_text(body, encoding="utf-8")
    venv = src / ".venv" / "Scripts"
    venv.mkdir(parents=True, exist_ok=True)
    (venv / "python.exe").write_text("venv-marker", encoding="utf-8")
    return src


def test_sync_plugin_first_install_seeds_and_writes_baseline(
    tmp_path: Path, mod: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """首装态：无基线、副本缺席 → 解包 + 基线立目 + 冲突文件不写。"""
    repo = _fake_repo(tmp_path)
    _repo_plugin(repo, version="1.0.0")
    monkeypatch.setattr(mod, "_REPO_ROOT", repo)
    user_root = tmp_path / "user_root"

    warnings = mod.sync_plugin("shared/demo_plugin", user_root, dry_run=False)

    assert warnings == []
    dst = user_root / "plugins" / "demo_plugin"
    assert (dst / "plugin.json").is_file()
    assert (dst / ".venv" / "Scripts" / "python.exe").read_text(encoding="utf-8") == "venv-marker"
    baseline = json.loads((user_root / mod.BASELINE_FILENAME).read_text(encoding="utf-8"))
    assert baseline["schema"] == 1
    entry = baseline["plugins"]["demo"]
    assert entry["tier"] == 2
    assert entry["version"] == "1.0.0"
    assert "server.py" in entry["files"], "基线立目必须含全树哈希"
    assert not (user_root / mod.CONFLICTS_FILENAME).exists(), "无冲突不写告警面"


def test_sync_plugin_unchanged_overwrites_and_refreshes_baseline(
    tmp_path: Path, mod: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """未改态：副本与基线一致 → 覆盖 + 基线翻新为仓内新版本。"""
    repo = _fake_repo(tmp_path)
    _repo_plugin(repo, version="1.0.0")
    monkeypatch.setattr(mod, "_REPO_ROOT", repo)
    user_root = tmp_path / "user_root"
    mod.sync_plugin("shared/demo_plugin", user_root, dry_run=False)

    # 仓内升版 + 改内容
    src = _repo_plugin(repo, version="1.1.0", body="print('v2')\n")

    warnings = mod.sync_plugin("shared/demo_plugin", user_root, dry_run=False)

    assert warnings == []
    dst = user_root / "plugins" / "demo_plugin"
    assert (dst / "server.py").read_text(encoding="utf-8") == "print('v2')\n", "未改态必须覆盖"
    assert json.loads((dst / "plugin.json").read_text(encoding="utf-8"))["version"] == "1.1.0"
    baseline = json.loads((user_root / mod.BASELINE_FILENAME).read_text(encoding="utf-8"))
    assert baseline["plugins"]["demo"]["version"] == "1.1.0", "基线条目翻新"
    assert baseline["plugins"]["demo"]["files"] == mod._hash_plugin_tree(src), "基线哈希=新仓树"
    assert not (user_root / mod.CONFLICTS_FILENAME).exists()


def test_sync_plugin_user_modified_keeps_and_writes_conflict(
    tmp_path: Path, mod: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """改过态：副本偏离基线 → 保留用户版 + 冲突记录（seed/user/changed_files）。"""
    repo = _fake_repo(tmp_path)
    _repo_plugin(repo, version="1.0.0")
    monkeypatch.setattr(mod, "_REPO_ROOT", repo)
    user_root = tmp_path / "user_root"
    mod.sync_plugin("shared/demo_plugin", user_root, dry_run=False)

    # 用户改副本 + 仓内升版
    dst = user_root / "plugins" / "demo_plugin"
    (dst / "server.py").write_text("USER_EDIT = True\n", encoding="utf-8")
    _repo_plugin(repo, version="1.2.0", body="print('v3')\n")

    warnings = mod.sync_plugin("shared/demo_plugin", user_root, dry_run=False)

    assert warnings == []
    assert (dst / "server.py").read_text(encoding="utf-8") == "USER_EDIT = True\n", "改过态绝不覆盖"
    assert json.loads((dst / "plugin.json").read_text(encoding="utf-8"))["version"] == "1.0.0"
    conflicts = json.loads((user_root / mod.CONFLICTS_FILENAME).read_text(encoding="utf-8"))
    assert conflicts["schema"] == 1
    assert len(conflicts["conflicts"]) == 1
    rec = conflicts["conflicts"][0]
    assert rec["plugin_id"] == "demo"
    assert rec["seed_version"] == "1.2.0", "seed_version=本轮仓内版本"
    assert rec["user_version"] == "1.0.0"
    assert rec["changed_files"] == ["server.py"]
    # 基线条目保持旧值（后续轮仍可对账）
    baseline = json.loads((user_root / mod.BASELINE_FILENAME).read_text(encoding="utf-8"))
    assert baseline["plugins"]["demo"]["version"] == "1.0.0"


def test_sync_plugin_no_baseline_evidence_keeps_existing_copy(
    tmp_path: Path, mod: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """无基线证据而副本在（前代残留）→ 保留 + 冲突留痕，绝不覆盖。"""
    repo = _fake_repo(tmp_path)
    _repo_plugin(repo, version="1.0.0")
    monkeypatch.setattr(mod, "_REPO_ROOT", repo)
    user_root = tmp_path / "user_root"
    dst = user_root / "plugins" / "demo_plugin"
    dst.mkdir(parents=True)
    (dst / "plugin.json").write_text(
        json.dumps({"id": "demo", "version": "0.9.0"}), encoding="utf-8"
    )
    (dst / "server.py").write_text("LEGACY = 1\n", encoding="utf-8")

    mod.sync_plugin("shared/demo_plugin", user_root, dry_run=False)

    assert (dst / "server.py").read_text(encoding="utf-8") == "LEGACY = 1\n"
    conflicts = json.loads((user_root / mod.CONFLICTS_FILENAME).read_text(encoding="utf-8"))
    assert conflicts["conflicts"][0]["user_version"] == "0.9.0"
    assert not (user_root / mod.BASELINE_FILENAME).exists(), "保留态不立基线"


def test_sync_plugin_refuses_tier1(
    tmp_path: Path, mod: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tier1 拒动：清单内 id（按 manifest id 或目录名命中）拒绝写入用户目录。"""
    repo = _fake_repo(tmp_path, tier1_ids=["demo"])
    _repo_plugin(repo, version="9.9.9")
    monkeypatch.setattr(mod, "_REPO_ROOT", repo)
    user_root = tmp_path / "user_root"

    with pytest.raises(SystemExit, match="Tier1"):
        mod.sync_plugin("shared/demo_plugin", user_root, dry_run=False)

    assert not (user_root / "plugins" / "demo_plugin").exists()
    assert not (user_root / mod.BASELINE_FILENAME).exists()

    # 目录名命中同样拒（id 缺失回落目录名的插件形态）
    repo2 = _fake_repo(tmp_path, tier1_ids=["demo_plugin"])
    src2 = repo2 / "plugins" / "shared" / "demo_plugin"
    src2.mkdir(parents=True, exist_ok=True)
    (src2 / "plugin.json").write_text(json.dumps({"version": "1.0.0"}), encoding="utf-8")
    monkeypatch.setattr(mod, "_REPO_ROOT", repo2)
    with pytest.raises(SystemExit, match="Tier1"):
        mod.sync_plugin("shared/demo_plugin", tmp_path / "ur2", dry_run=False)


def test_sync_plugin_refuses_on_corrupt_baseline(
    tmp_path: Path, mod: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """基线损坏 → 拒绝覆盖性动作（不可读证据不删，与安装器同语义）。"""
    repo = _fake_repo(tmp_path)
    _repo_plugin(repo)
    monkeypatch.setattr(mod, "_REPO_ROOT", repo)
    user_root = tmp_path / "user_root"
    user_root.mkdir(parents=True)
    (user_root / mod.BASELINE_FILENAME).write_text("{broken", encoding="utf-8")

    with pytest.raises(SystemExit, match="plugin-baseline.json"):
        mod.sync_plugin("shared/demo_plugin", user_root, dry_run=False)
    assert (user_root / mod.BASELINE_FILENAME).read_text(encoding="utf-8") == "{broken"


def test_sync_plugin_dry_run_writes_nothing(
    tmp_path: Path, mod: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """dry-run：四态照常打印决策，但零落盘（副本/基线/冲突全不写）。"""
    repo = _fake_repo(tmp_path)
    _repo_plugin(repo)
    monkeypatch.setattr(mod, "_REPO_ROOT", repo)
    user_root = tmp_path / "user_root"

    mod.sync_plugin("shared/demo_plugin", user_root, dry_run=True)

    assert not (user_root / "plugins" / "demo_plugin").exists()
    assert not (user_root / mod.BASELINE_FILENAME).exists()
    assert not (user_root / mod.CONFLICTS_FILENAME).exists()


def test_check_import_closure_accepts_manifestless_shared_module(
    tmp_path: Path, mod: Any
) -> None:
    """无 manifest 共享裸模块（_root_api 型）的内置目录也算合法闭包副本。

    2026-10-23 实证：trace_exporter ``from _root_api import …``，闭包闸只认
    plugin.json → 内置 shared 根里的 _root_api 被无视，sync 恒报不满足。
    """
    src = tmp_path / "repo" / "plugins" / "shared" / "trace_exporter"
    src.mkdir(parents=True)
    (src / "server.py").write_text(
        "from _root_api import now_iso_utc\n", encoding="utf-8"
    )
    builtin_shared = tmp_path / "ins" / "resources" / "plugins" / "shared"
    (builtin_shared / "_root_api").mkdir(parents=True)
    (builtin_shared / "_root_api" / "__init__.py").write_text(
        "def now_iso_utc(): ...\n", encoding="utf-8"
    )
    (builtin_shared / "trace_exporter").mkdir(parents=True)
    (builtin_shared / "trace_exporter" / "plugin.json").write_text(
        json.dumps({"id": "trace_exporter", "version": "1.0.0"}), encoding="utf-8"
    )

    reason = mod.check_import_closure(
        src, tmp_path / "user" / "plugins", install_roots=[tmp_path / "ins"]
    )
    assert reason is None, f"无 manifest 共享件应通过闭包闸，实际: {reason}"


def test_check_import_closure_still_fails_when_dep_missing_everywhere(
    tmp_path: Path, mod: Any
) -> None:
    """依赖目录两侧皆无 → 维持 fail-closed 报不满足。"""
    src = tmp_path / "repo" / "plugins" / "shared" / "trace_exporter"
    src.mkdir(parents=True)
    (src / "server.py").write_text(
        "from _root_api import now_iso_utc\n", encoding="utf-8"
    )
    install = tmp_path / "ins"
    (install / "resources" / "plugins" / "shared").mkdir(parents=True)

    reason = mod.check_import_closure(
        src, tmp_path / "user" / "plugins", install_roots=[install]
    )
    assert reason is not None
    assert "均未找到" in reason


def test_existing_install_dirs_env_override(tmp_path: Path, mod: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """自定义安装位经 AGENTOS_INSTALL_DIR 追加（NSIS 卸载注册表无 InstallLocation）。"""
    custom = tmp_path / "custom-install"
    custom.mkdir()
    monkeypatch.setenv("AGENTOS_INSTALL_DIR", str(custom))
    dirs = mod._existing_install_dirs()
    assert custom.resolve() in dirs

    monkeypatch.setenv("AGENTOS_INSTALL_DIR", str(tmp_path / "nope"))
    assert tmp_path / "nope" not in list(mod._existing_install_dirs())


def test_check_import_closure_venv_editable_probe(tmp_path: Path, mod: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """依赖无副本形态（editable path 装配）→ 回落源侧 venv 真实探测。"""
    src = tmp_path / "repo" / "plugins" / "shared" / "trace_exporter"
    src.mkdir(parents=True)
    (src / "server.py").write_text(
        "from agentos_plugin_sdk import AgentOSPlugin\n", encoding="utf-8"
    )
    venv_scripts = src / ".venv" / "Scripts"
    venv_scripts.mkdir(parents=True)
    (venv_scripts / "python.exe").write_text("venv-marker", encoding="utf-8")

    captured: dict[str, Any] = {}

    class _Proc:
        stdout = "[]"
        returncode = 0

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["cwd"] = kwargs.get("cwd")
        return _Proc()

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    install = tmp_path / "ins"
    (install / "resources" / "plugins" / "shared").mkdir(parents=True)

    reason = mod.check_import_closure(
        src, tmp_path / "user" / "plugins", install_roots=[install]
    )
    assert reason is None
    assert captured["argv"][0].endswith(("python.exe", "python")), "必须用源侧 venv 解释器探测"
    assert captured["cwd"] is None, "venv 探测不落 shared 根 cwd（防裸名误命中）"


def test_check_import_closure_no_dep_and_no_venv_reports_missing(tmp_path: Path, mod: Any) -> None:
    """无副本且源侧无 venv → 维持 fail-closed。"""
    src = tmp_path / "repo" / "plugins" / "shared" / "trace_exporter"
    src.mkdir(parents=True)
    (src / "server.py").write_text(
        "from agentos_plugin_sdk import AgentOSPlugin\n", encoding="utf-8"
    )
    install = tmp_path / "ins"
    (install / "resources" / "plugins" / "shared").mkdir(parents=True)

    reason = mod.check_import_closure(
        src, tmp_path / "user" / "plugins", install_roots=[install]
    )
    assert reason is not None
    assert "均未找到" in reason


def test_check_import_closure_skips_in_plugin_modules(tmp_path: Path, mod: Any) -> None:
    """插件目录内裸模块（trace_export_core 型）不进跨插件依赖收集。"""
    src = tmp_path / "repo" / "plugins" / "shared" / "trace_exporter"
    src.mkdir(parents=True)
    (src / "server.py").write_text(
        "from trace_export_core import OutboxStore\n", encoding="utf-8"
    )
    (src / "trace_export_core.py").write_text(
        "class OutboxStore: ...\n", encoding="utf-8"
    )
    install = tmp_path / "ins"
    (install / "resources" / "plugins" / "shared").mkdir(parents=True)

    reason = mod.check_import_closure(
        src, tmp_path / "user" / "plugins", install_roots=[install]
    )
    assert reason is None, f"插件内模块不得判为跨插件依赖，实际: {reason}"


def _make_manifest(plugin_dir: Path, pid: str, version: str) -> Path:
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "plugin.json").write_text(
        json.dumps({"id": pid, "version": version}), encoding="utf-8"
    )
    return plugin_dir


@pytest.fixture
def prune_env(tmp_path: Path) -> tuple[Path, Path]:
    """(user_root, install_root)：种子 alpha 2.0 / beta 1.0。"""
    user_root = tmp_path / "user"
    install = tmp_path / "ins"
    shared = install / "resources" / "plugins" / "shared"
    _make_manifest(shared / "alpha", "alpha_tool", "2.0.0")
    _make_manifest(shared / "beta", "beta_tool", "1.0.0")
    return user_root, install


# ── --prune-stale：单副本模型（影子判据随双源仲裁退役，ADR 2026-11-07 §2.5）──


def test_prune_stale_keeps_seed_named_copy_regardless_of_version(
    prune_env: tuple[Path, Path], mod: Any
) -> None:
    """种子仍有同名 id 的副本一律不动（版本无关）：运行期真值无「影子」概念。

    退役回归钉：旧 BuiltinFirst 判据（版本 <= 种子 = 隔离）会误删运行期
    唯一副本，必须保持退役（判据移交基线对账）。
    """
    user_root, install = prune_env
    _make_manifest(user_root / "plugins" / "alpha_old", "alpha_tool", "1.0.0")
    _make_manifest(user_root / "plugins" / "alpha_equal", "alpha_tool", "2.0.0")

    moved = mod.prune_stale_plugins(user_root, install_roots=[install])

    assert moved == [], "种子有同名 id 的副本不得被任何版本判据处置"
    assert (user_root / "plugins" / "alpha_old" / "plugin.json").is_file()
    assert (user_root / "plugins" / "alpha_equal" / "plugin.json").is_file()
    assert not list(user_root.glob(f"{mod.QUARANTINE_PREFIX}*"))


def test_prune_stale_keeps_orphan_user_plugin(
    prune_env: tuple[Path, Path], mod: Any
) -> None:
    """内置无同名 id 的用户自有插件不动（只报告）。"""
    user_root, install = prune_env
    _make_manifest(user_root / "plugins" / "mine", "my_own_plugin", "0.1.0")

    moved = mod.prune_stale_plugins(user_root, install_roots=[install])

    assert moved == []
    assert (user_root / "plugins" / "mine" / "plugin.json").is_file()


def test_prune_stale_dry_run_moves_nothing(
    prune_env: tuple[Path, Path], mod: Any
) -> None:
    """dry-run 只打印计划，不落盘、不建隔离目录。"""
    user_root, install = prune_env
    _make_manifest(user_root / "plugins" / "alpha_old", "alpha_tool", "1.0.0")

    moved = mod.prune_stale_plugins(user_root, dry_run=True, install_roots=[install])

    assert moved == []
    assert (user_root / "plugins" / "alpha_old" / "plugin.json").is_file()
    assert not list(user_root.glob(f"{mod.QUARANTINE_PREFIX}*"))


def test_prune_stale_without_install_roots_is_noop(tmp_path: Path, mod: Any) -> None:
    """找不到装机安装位 → 判不了影子，跳过且不误删。"""
    user_root = tmp_path / "user"
    _make_manifest(user_root / "plugins" / "alpha_old", "alpha_tool", "1.0.0")

    moved = mod.prune_stale_plugins(user_root, install_roots=[])

    assert moved == []
    assert (user_root / "plugins" / "alpha_old" / "plugin.json").is_file()


def test_prune_stale_handles_nested_group_layout(
    prune_env: tuple[Path, Path], mod: Any
) -> None:
    """两层布局 plugins/<组>/<id>/ 同样识别（种子同名 id 副本不动）。"""
    user_root, install = prune_env
    _make_manifest(
        user_root / "plugins" / "legacy_group" / "alpha_mode", "alpha_tool", "2.0.0"
    )

    moved = mod.prune_stale_plugins(user_root, install_roots=[install])

    assert moved == []
    assert (user_root / "plugins" / "legacy_group" / "alpha_mode" / "plugin.json").is_file()


# ── --prune-stale：孤儿甄别 + modes 豁免（E4-P0 回填 2026-11-05 装机现场判据）──


class TestOrphanImportSmoke:
    """孤儿（内置无同名 id）的 import 冒烟甄别：失败隔离 / 健康保留。"""

    def test_orphan_with_failed_import_gets_quarantined(
        self,
        prune_env: tuple[Path, Path],
        mod: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """孤儿且 import 冒烟失败 → 隔离，why 以「内置已下架且 import 失败」开头。

        冒烟 subprocess 真跑当前解释器（真实依赖）：server.py 引不存在的
        模块必炸，重现 E3 遗孤形态（共享根断裂恒炸）。
        """
        user_root, install = prune_env
        orphan = _make_manifest(
            user_root / "plugins" / "legacy_orphan", "legacy_orphan", "1.0.0"
        )
        (orphan / "server.py").write_text(
            "import nonexistent_module_xyz\n", encoding="utf-8"
        )
        monkeypatch.setattr(mod, "find_venv_python", lambda _d: sys.executable)

        moved = mod.prune_stale_plugins(user_root, install_roots=[install])

        assert [m["id"] for m in moved] == ["legacy_orphan"]
        assert not (user_root / "plugins" / "legacy_orphan").exists()
        quarantined = list(
            user_root.glob(f"{mod.QUARANTINE_PREFIX}*/plugins/legacy_orphan")
        )
        assert quarantined, "必须移入隔离区而非删除（可回滚）"
        manifest = json.loads(
            (quarantined[0].parents[1] / "prune-manifest.json").read_text(encoding="utf-8")
        )
        assert manifest[0]["why"].startswith("种子已下架且 import 失败")

    def test_orphan_with_healthy_import_is_kept(
        self,
        prune_env: tuple[Path, Path],
        mod: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """孤儿且 import 冒烟通过 → 保留（用户自有健康插件，只报告）。"""
        user_root, install = prune_env
        orphan = _make_manifest(
            user_root / "plugins" / "mine", "my_own_plugin", "0.1.0"
        )
        (orphan / "server.py").write_text("VALUE = 1\n", encoding="utf-8")
        monkeypatch.setattr(mod, "find_venv_python", lambda _d: sys.executable)

        moved = mod.prune_stale_plugins(user_root, install_roots=[install])

        assert moved == []
        assert (user_root / "plugins" / "mine" / "server.py").is_file()

    def test_orphan_without_venv_is_kept(
        self, prune_env: tuple[Path, Path], mod: Any
    ) -> None:
        """无 venv 解释器 → 冒烟判不了，保守保留（宁可漏不可错杀）。"""
        user_root, install = prune_env
        _make_manifest(user_root / "plugins" / "novenv_orphan", "novenv_orphan", "1.0.0")

        moved = mod.prune_stale_plugins(user_root, install_roots=[install])

        assert moved == []
        assert (user_root / "plugins" / "novenv_orphan" / "plugin.json").is_file()


class TestModesSubtreeExemption:
    """modes 子树恒用户赢（ADR 2026-09-20）：不进 prune 影子/孤儿处置。"""

    def test_iter_plugin_manifests_skips_modes_subtree(
        self, tmp_path: Path, mod: Any
    ) -> None:
        """skip_modes=True：modes 子树（含带 manifest 的模式包）不入遍历。"""
        root = tmp_path / "plugins"
        _make_manifest(root / "top_tool", "top_tool", "1.0.0")
        _make_manifest(root / "modes" / "mode_a", "mode_a_tool", "1.0.0")
        _make_manifest(root / "modes" / "mode_b", "mode_b_tool", "1.0.0")

        yielded = {
            p.name for p in mod._iter_plugin_manifests(root, skip_modes=True)
        }

        assert yielded == {"top_tool"}

    def test_prune_stale_never_touches_modes_subtree(
        self,
        prune_env: tuple[Path, Path],
        mod: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """影子形态（<=内置）与孤儿 import 失败两种形态都不得处置 modes 副本。"""
        user_root, install = prune_env
        _make_manifest(
            user_root / "plugins" / "modes" / "alpha_mode", "alpha_tool", "2.0.0"
        )
        broken = _make_manifest(
            user_root / "plugins" / "modes" / "broken_mode", "broken_mode", "1.0.0"
        )
        (broken / "server.py").write_text("import nonexistent_module_xyz\n", encoding="utf-8")
        monkeypatch.setattr(mod, "find_venv_python", lambda _d: sys.executable)

        moved = mod.prune_stale_plugins(user_root, install_roots=[install])

        assert moved == []
        assert (user_root / "plugins" / "modes" / "alpha_mode" / "plugin.json").is_file()
        assert (user_root / "plugins" / "modes" / "broken_mode" / "server.py").is_file()


class TestPostCopyVerify:
    """复制后回读（ADR 2026-11-10-deploy-freshness-verification）。

    「同步动作打印了」不等于「文件真的到位」——三类复制（plugin/config/skills）
    结束都必须重算目标哈希，不一致即 [VERIFY-FAIL] + exit 1。
    """

    def test_verify_mirror_equal_then_tampered(self, tmp_path: Path, mod: Any) -> None:
        src = _make_plugin(tmp_path)
        dst = tmp_path / "dst" / "demo_plugin"
        mod._mirror_tree(src, dst)

        checked, problems = mod._verify_mirror(src, dst)
        assert problems == []
        assert checked >= 2  # .venv/__pycache__ 不在比对面

        (dst / "server.py").write_text("print('stale')\n", encoding="utf-8")
        _, problems = mod._verify_mirror(src, dst)
        assert problems == ["server.py"]

    def test_verify_mirror_missing_dst_lists_all(self, tmp_path: Path, mod: Any) -> None:
        src = _make_plugin(tmp_path)
        checked, problems = mod._verify_mirror(src, tmp_path / "absent")
        assert checked >= 2
        assert len(problems) == checked

    def test_assert_mirror_verified_fails_closed(self, tmp_path: Path, mod: Any) -> None:
        src = _make_plugin(tmp_path)
        dst = tmp_path / "dst" / "demo_plugin"
        mod._mirror_tree(src, dst)
        (dst / "extra.py").write_text("x", encoding="utf-8")
        with pytest.raises(SystemExit, match="VERIFY-FAIL"):
            mod._assert_mirror_verified(src, dst, "插件 demo")

    def _fake_repo(self, tmp_path: Path, mod: Any, monkeypatch: pytest.MonkeyPatch) -> Path:
        repo = tmp_path / "repo"
        plugin = repo / "plugins" / "demo_plugin"
        (plugin / "sub").mkdir(parents=True)
        (plugin / "plugin.json").write_text('{"id": "demo", "version": "1.0.0"}', encoding="utf-8")
        (plugin / "server.py").write_text("print(1)\n", encoding="utf-8")
        (plugin / "sub" / "helper.py").write_text("X = 1\n", encoding="utf-8")
        tiers = repo / "config" / "kernel" / "plugin_tiers.yaml"
        tiers.parent.mkdir(parents=True)
        tiers.write_text("tier1:\n  safety_plugins: []\n  shared_modules: []\n", encoding="utf-8")
        monkeypatch.setattr(mod, "_REPO_ROOT", repo)
        monkeypatch.setattr(mod, "_provision_venv", lambda *a, **k: "venv skipped")
        monkeypatch.setattr(mod, "check_import_closure", lambda *a, **k: [])
        return repo

    def test_sync_plugin_verifies_after_copy(
        self, tmp_path: Path, mod: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._fake_repo(tmp_path, mod, monkeypatch)
        user_root = tmp_path / "user_root"
        user_root.mkdir()

        mod.sync_plugin("demo_plugin", user_root, dry_run=False)
        assert (user_root / "plugins" / "demo_plugin" / "server.py").is_file()

        # 二次调用走「副本与基线一致 → 覆盖」分支，同样过回读
        mod.sync_plugin("demo_plugin", user_root, dry_run=False)

    def test_sync_plugin_fails_closed_when_copy_missed(
        self, tmp_path: Path, mod: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._fake_repo(tmp_path, mod, monkeypatch)
        user_root = tmp_path / "user_root"
        user_root.mkdir()
        monkeypatch.setattr(mod, "_mirror_tree", lambda *a, **k: [])

        with pytest.raises(SystemExit, match="VERIFY-FAIL"):
            mod.sync_plugin("demo_plugin", user_root, dry_run=False)

    def test_sync_config_verifies_bytes(
        self, tmp_path: Path, mod: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = tmp_path / "repo"
        cfg = repo / "config" / "pipelines" / "x.yaml"
        cfg.parent.mkdir(parents=True)
        cfg.write_text("a: 1\n", encoding="utf-8")
        monkeypatch.setattr(mod, "_REPO_ROOT", repo)
        user_root = tmp_path / "user_root"

        mod.sync_config("pipelines/x.yaml", user_root, dry_run=False)
        assert (user_root / "config" / "pipelines" / "x.yaml").read_text(encoding="utf-8") == "a: 1\n"

        cfg.write_text("a: 2\n", encoding="utf-8")
        monkeypatch.setattr(mod.shutil, "copy2", lambda *a, **k: None)  # 模拟复制未生效
        with pytest.raises(SystemExit, match="VERIFY-FAIL"):
            mod.sync_config("pipelines/x.yaml", user_root, dry_run=False)

    def test_sync_skills_verifies_mirror(
        self, tmp_path: Path, mod: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = tmp_path / "repo"
        skill = repo / "skills" / "demo"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("# demo\n", encoding="utf-8")
        monkeypatch.setattr(mod, "_REPO_ROOT", repo)
        user_root = tmp_path / "user_root"

        mod.sync_skills("demo", user_root, dry_run=False)
        assert (user_root / "skills" / "demo" / "SKILL.md").is_file()

        (skill / "SKILL.md").write_text("# demo v2\n", encoding="utf-8")
        monkeypatch.setattr(mod, "_mirror_tree", lambda *a, **k: [])  # 模拟复制未生效
        with pytest.raises(SystemExit, match="VERIFY-FAIL"):
            mod.sync_skills("demo", user_root, dry_run=False)
