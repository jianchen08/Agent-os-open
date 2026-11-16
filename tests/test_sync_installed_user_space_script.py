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


# ── D4（审查总册 P1-15）：版本生效检查 + 未生效 id 清单 ──


@pytest.mark.parametrize(
    ("raw", "expected_major"),
    [
        ("1.2.3", 1),  # 组 A：纯 semver
        ("v1.2.3", 1),  # 组 B：小写 v 前缀（sync 链常见写坏形态）
        ("V1.2.3", 1),  # 组 B：大写 V 前缀
        ("  1.2.3  ", 1),  # 首尾空白
        ("1.2.3-rc.1+build.7", 1),  # 预发布 + 构建元数据
    ],
)
def test_parse_semver_lenient_accepts(raw: str, expected_major: int, mod: Any) -> None:
    key = mod.parse_semver_lenient(raw)
    assert key is not None, f"{raw!r} 必须宽容解析成功"
    assert key[0] == expected_major


@pytest.mark.parametrize("raw", ["not-semver", "1.2", "v1", "1.2.x", "", "version 1.2.3"])
def test_parse_semver_lenient_rejects(raw: str, mod: Any) -> None:
    assert mod.parse_semver_lenient(raw) is None, f"{raw!r} 必须判不可解析"


def test_parse_semver_lenient_ordering_properties(mod: Any) -> None:
    """性质断言：strip 前缀后与纯 semver 同序；正式版 > 预发布；位数比较。"""
    assert mod.parse_semver_lenient("v1.0.0") == mod.parse_semver_lenient("1.0.0")
    assert mod.parse_semver_lenient("v1.10.0") > mod.parse_semver_lenient("1.9.0")
    assert mod.parse_semver_lenient("1.0.0") > mod.parse_semver_lenient("1.0.0-rc.1")
    assert mod.parse_semver_lenient("1.0.0-rc.2") > mod.parse_semver_lenient("1.0.0-rc.1")
    assert mod.parse_semver_lenient("1.0.0-rc.1") < mod.parse_semver_lenient("1.0.0-rc.1.1")
    assert mod.parse_semver_lenient("0.2.0") > mod.parse_semver_lenient("v0.1.9")


def _make_plugin_with_version(src: Path, version: str) -> Path:
    """_make_plugin 基础上写 version 字段（D4 生效检查用）。"""
    src = _make_plugin(src)
    data = {"id": "demo", "version": version}
    (src / "plugin.json").write_text(json.dumps(data), encoding="utf-8")
    return src


def _make_install_dir(tmp_path: Path, version: str | None) -> Path:
    """假装机包安装位 + 内置副本（resources/plugins/shared 布局）。"""
    install = tmp_path / "inst" / "agent-os"
    builtin = install / "resources" / "plugins" / "shared" / "demo_plugin"
    builtin.mkdir(parents=True)
    manifest: dict[str, Any] = {"id": "demo"}
    if version is not None:
        manifest["version"] = version
    (builtin / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
    return install


def test_check_version_ineffective_when_builtin_newer_or_equal(
    tmp_path: Path, mod: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """组 A/B 区分度输入：纯 semver 更旧与 v 前缀更旧都必须判未生效。"""
    for src_version, builtin_version in [("0.9.0", "1.0.0"), ("v0.2.0", "0.2.0"), ("v1.0.0", "1.0.0")]:
        src = _make_plugin_with_version(tmp_path / f"src_{src_version}_{builtin_version}", src_version)
        install = _make_install_dir(tmp_path / f"ins_{src_version}_{builtin_version}", builtin_version)
        monkeypatch.setattr(mod, "_existing_install_dirs", lambda install=install: [install])
        reason = mod.check_version_effective(src)
        assert reason is not None, f"源 {src_version} vs 内置 {builtin_version} 必须判未生效"
        assert src_version in reason, "源版本号须如实透出"
        assert builtin_version in reason, "内置版本号须如实透出"


def test_check_version_effective_when_strictly_newer(
    tmp_path: Path, mod: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """v 前缀 strip 后严格大于内置 → 生效（None），修复路径可见。"""
    src = _make_plugin_with_version(tmp_path / "src", "v1.1.0")
    install = _make_install_dir(tmp_path / "ins", "1.0.0")
    monkeypatch.setattr(mod, "_existing_install_dirs", lambda: [install])
    assert mod.check_version_effective(src) is None


def test_check_version_flags_unparseable_without_builtin(
    tmp_path: Path, mod: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """无装机包（比较跳过）时源版本写坏仍必须告警——静默失效零容忍。"""
    src = _make_plugin_with_version(tmp_path / "src", "v broken")
    monkeypatch.setattr(mod, "_existing_install_dirs", lambda: [])
    reason = mod.check_version_effective(src)
    assert reason is not None, "写坏版本号必须告警"
    assert "非 semver" in reason


def test_check_version_no_builtin_skips_comparison(
    tmp_path: Path, mod: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """装机包未装（找不到内置副本）→ 无法判定，不入清单。"""
    src = _make_plugin_with_version(tmp_path / "src", "0.1.0")
    monkeypatch.setattr(mod, "_existing_install_dirs", lambda: [])
    assert mod.check_version_effective(src) is None


def test_check_version_builtin_unparseable_reports_ineffective(
    tmp_path: Path, mod: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """内置版本不可解析 → 仲裁保守判内置赢 → 未生效（与内核逐位同源）。"""
    src = _make_plugin_with_version(tmp_path / "src", "9.9.9")
    install = _make_install_dir(tmp_path / "ins", "what-version")
    monkeypatch.setattr(mod, "_existing_install_dirs", lambda: [install])
    reason = mod.check_version_effective(src)
    assert reason is not None, "内置版本不可解析必须判未生效"
    assert "不可解析" in reason


def test_sync_plugin_returns_reason_and_warns(
    tmp_path: Path, mod: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """sync_plugin 端到端：镜像照常执行，同时返回未生效原因并打 [WARN]。"""
    monkeypatch.setattr(mod, "_REPO_ROOT", tmp_path / "repo")
    src = tmp_path / "repo" / "plugins" / "shared" / "demo_plugin"
    src.mkdir(parents=True)
    (src / "plugin.json").write_text(
        json.dumps({"id": "demo", "version": "v0.2.0"}), encoding="utf-8"
    )
    # 最小 venv 桩（源侧有 .venv → 整树复制分支，不触发 uv 现建子进程）
    (src / ".venv" / "Scripts").mkdir(parents=True)
    (src / ".venv" / "Scripts" / "python.exe").write_text("venv-marker", encoding="utf-8")
    install = _make_install_dir(tmp_path / "ins", "0.2.0")
    monkeypatch.setattr(mod, "_existing_install_dirs", lambda: [install])

    reason = mod.sync_plugin("shared/demo_plugin", tmp_path / "user_root", dry_run=False)

    assert reason is not None, "未生效必须返回原因"
    assert "demo_plugin" in reason
    out = capsys.readouterr().out
    assert "[WARN] 未生效" in out, "失效必须显式可见"
    assert (tmp_path / "user_root" / "plugins" / "demo_plugin" / "plugin.json").exists(), "镜像照常执行"


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


# ── --prune-stale：BuiltinFirst 影子副本隔离 ────────────────────────────────


def _make_manifest(plugin_dir: Path, pid: str, version: str) -> Path:
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "plugin.json").write_text(
        json.dumps({"id": pid, "version": version}), encoding="utf-8"
    )
    return plugin_dir


@pytest.fixture
def prune_env(tmp_path: Path) -> tuple[Path, Path]:
    """(user_root, install_root)：内置 alpha 2.0 / beta 1.0。"""
    user_root = tmp_path / "user"
    install = tmp_path / "ins"
    shared = install / "resources" / "plugins" / "shared"
    _make_manifest(shared / "alpha", "alpha_tool", "2.0.0")
    _make_manifest(shared / "beta", "beta_tool", "1.0.0")
    return user_root, install


def test_prune_stale_moves_shadow_and_keeps_user_override(
    prune_env: tuple[Path, Path], mod: Any
) -> None:
    """旧/持平版本影子被隔离；严格高于内置的用户覆盖保留。"""
    user_root, install = prune_env
    _make_manifest(user_root / "plugins" / "alpha_old", "alpha_tool", "1.0.0")
    _make_manifest(user_root / "plugins" / "beta_new", "beta_tool", "1.1.0")

    moved = mod.prune_stale_plugins(user_root, install_roots=[install])

    assert [m["id"] for m in moved] == ["alpha_tool"]
    assert not (user_root / "plugins" / "alpha_old").exists()
    assert (user_root / "plugins" / "beta_new" / "plugin.json").is_file(), "严格更新版必须保留"
    quarantined = list(user_root.glob(f"{mod.QUARANTINE_PREFIX}*/plugins/alpha_old"))
    assert quarantined, "影子必须移入隔离区而非删除"
    assert quarantined[0].is_dir()
    manifest = json.loads((quarantined[0].parents[1] / "prune-manifest.json").read_text(encoding="utf-8"))
    assert manifest[0]["id"] == "alpha_tool"
    assert manifest[0]["version"] == "1.0.0"


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
    """两层布局 plugins/<组>/<id>/ 同样识别（modes 子树另受恒用户赢豁免）。"""
    user_root, install = prune_env
    _make_manifest(
        user_root / "plugins" / "legacy_group" / "alpha_mode", "alpha_tool", "2.0.0"
    )

    moved = mod.prune_stale_plugins(user_root, install_roots=[install])

    assert [m["dir"] for m in moved] == ["alpha_mode"]
    assert not (user_root / "plugins" / "legacy_group" / "alpha_mode").exists()


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
        assert manifest[0]["why"].startswith("内置已下架且 import 失败")

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
