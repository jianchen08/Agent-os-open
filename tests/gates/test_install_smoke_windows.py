# @feature: FP-0.2.PACK 打包/部署 CI 门 | @ci: python-test
"""install_smoke_windows.py 纯函数层单测（静默安装 argv 与随包契约断言）。

行为契约（断输入→输出，不装真安装器不起真内核）：
- 静默安装 argv：/S 与 /D=<目录> 形态（NSIS /D 必带否则零文件 exit 0 的
  装机坑回归面）；
- 随包契约：缺文件/文件过小/缺目录/目录过空逐项报出，齐全=空列表；
- resources 根解析：<安装目录>/resources 在位性。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
pytestmark = pytest.mark.unit


def _load():
    spec = importlib.util.spec_from_file_location(
        "install_smoke_windows", SCRIPTS / "install_smoke_windows.py"
    )
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # dataclass + future annotations 需模块在册
    spec.loader.exec_module(mod)
    return mod


mod = _load()


class TestInstallerArgv:
    def test_silent_with_explicit_dest_dir(self, tmp_path: Path):
        installer = tmp_path / "灵汐助手 Setup 0.2.2.exe"
        argv = mod.installer_argv(installer, tmp_path / "dest")
        assert argv[0] == str(installer)
        assert "/S" in argv
        assert f"/D={tmp_path / 'dest'}" in argv

    def test_argv_has_no_empty_flag(self, tmp_path: Path):
        argv = mod.installer_argv(tmp_path / "s.exe", tmp_path / "d")
        assert all(a.strip() for a in argv)


class TestMissingCarry:
    SPEC = [
        ("kernel/agentos-kernel.exe", "file", 10),
        ("plugins/shared", "dir", 2),
    ]

    def _fake_install(self, tmp_path: Path) -> Path:
        resources = tmp_path / "resources"
        (resources / "kernel").mkdir(parents=True)
        (resources / "kernel" / "agentos-kernel.exe").write_bytes(b"x" * 100)
        shared = resources / "plugins" / "shared"
        shared.mkdir(parents=True)
        (shared / "a").mkdir()
        (shared / "b").mkdir()
        return resources

    def test_complete_tree_passes(self, tmp_path: Path):
        assert mod.missing_carry(self._fake_install(tmp_path), self.SPEC) == []

    def test_missing_file_reported(self, tmp_path: Path):
        resources = self._fake_install(tmp_path)
        (resources / "kernel" / "agentos-kernel.exe").unlink()
        problems = mod.missing_carry(resources, self.SPEC)
        assert len(problems) == 1
        assert "缺文件" in problems[0]

    def test_undersized_file_reported(self, tmp_path: Path):
        resources = self._fake_install(tmp_path)
        (resources / "kernel" / "agentos-kernel.exe").write_bytes(b"x")
        problems = mod.missing_carry(resources, self.SPEC)
        assert any("过小" in p for p in problems)

    def test_missing_dir_reported(self, tmp_path: Path):
        resources = self._fake_install(tmp_path)
        import shutil

        shutil.rmtree(resources / "plugins")
        problems = mod.missing_carry(resources, self.SPEC)
        assert any("缺目录" in p for p in problems)

    def test_too_empty_dir_reported(self, tmp_path: Path):
        resources = self._fake_install(tmp_path)
        import shutil

        shutil.rmtree(resources / "plugins" / "shared" / "b")
        problems = mod.missing_carry(resources, self.SPEC)
        assert any("过空" in p for p in problems)

    def test_default_spec_covers_kernel_host_venv_and_offline_trio(self, tmp_path: Path):
        resources = tmp_path / "r"
        resources.mkdir()
        problems = mod.missing_carry(resources)
        joined = " ".join(problems)
        for must in ("agentos-kernel.exe", "_host/.venv", "prereqs/uv.exe",
                     "prereqs/wheels", "rclone.exe", "winfsp"):
            assert must in joined, f"默认契约未覆盖 {must}"


class TestFindResources:
    def test_missing_resources_raises(self, tmp_path: Path):
        with pytest.raises(FileNotFoundError):
            mod.find_resources(tmp_path)

    def test_present_resources_returned(self, tmp_path: Path):
        (tmp_path / "resources").mkdir()
        assert mod.find_resources(tmp_path) == tmp_path / "resources"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__]))
