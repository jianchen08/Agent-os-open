# @feature: FP-0.2.一 双根插件供给同步脚本 | @ci: python-coverage
"""sync_installed_user_space 脚本契约测试。

锁四个硬行为：镜像语义（复制+删源侧已消失+data/ 保留+排除名单）、venv 供给
（源 .venv 整树复制）、config 整覆盖、以及两条拒绝边界（无 --user-root 拒写 /
目标落装机包安装目录内拒写）。uv 现建分支依赖外部 uv，不进单测面。

加载经 importlib 唯名模块（scripts/ 非包，防裸名串扰）。
"""

from __future__ import annotations

import importlib.util
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
    """目标落在装机包安装目录子树内 → 拒绝（不动装机版硬边界）。"""
    fake_install = tmp_path / "Programs" / "agent-os"
    fake_install.mkdir(parents=True)
    monkeypatch.setattr(mod, "_existing_install_dirs", lambda: [fake_install])

    with pytest.raises(SystemExit, match="不动装机包"):
        mod.resolve_user_root(str(fake_install / "somewhere" / "user"))

    # 安装目录之外合法通过
    ok = mod.resolve_user_root(str(tmp_path / "real_user_root"))
    assert ok == (tmp_path / "real_user_root").resolve()
