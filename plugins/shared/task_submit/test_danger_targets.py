# @feature: FP-0.2.二 任务提交 | @ci: python-coverage
"""危险目标空间校验单源化测试（多端平台差异普查 ⚠️-4 收敛回归锚）。

锁定契约：
1. 盘符根判定 = 路径形态而非运行平台——task_submit 校验在 posix 运行时形态
   下对 "C:\\" 形态盘符根（含裸盘符 "D:"）同样必须拒。收敛前 task_submit 按
   ``os.name`` 判定（isolation 按形态），Linux 侧车上对 "C:\\" 形态漏判；
2. 危险清单单源：两消费方（isolation/task_submit）对清单逐目录拒绝一致，
   且与 ``_libs.dangerous_targets`` 字面清单一致（防再复制漂移）；
3. 形态判定不回归：具体子目录放行（与 isolation 既有断言同款对照）。
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_TASK_SUBMIT_DIR = Path(__file__).resolve().parent
_SHARED_ROOT = _TASK_SUBMIT_DIR.parent
_ISOLATION_DIR = _SHARED_ROOT / "isolation"
for _p in (_SHARED_ROOT, _TASK_SUBMIT_DIR, _ISOLATION_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import tool as _task_submit  # noqa: E402
from _libs import dangerous_targets as _dt  # noqa: E402


def _load_isolation_ws() -> Any:
    """加载 isolation/workspace.py（与其自身测试同款 importlib 装载）。"""
    mod_name = "isolation_workspace_danger_targets_test"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, _ISOLATION_DIR / "workspace.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


_ISOLATION_WS = _load_isolation_ws()

# 单源清单字面锚（收敛前 isolation/task_submit 逐字双份的原值）
_LITERAL_WINDOWS_DIRS = [
    r"C:\Windows",
    r"C:\Windows\System32",
    r"C:\Windows\SysWOW64",
    r"C:\Program Files",
    r"C:\Program Files (x86)",
    r"C:\ProgramData",
    r"C:\Users",
]
_LITERAL_UNIX_DIRS = [
    "/",
    "/bin",
    "/boot",
    "/dev",
    "/etc",
    "/lib",
    "/lib64",
    "/proc",
    "/root",
    "/sbin",
    "/sys",
    "/usr",
    "/var",
    "/tmp",
    "/home",
    "/opt",
]


class TestDriveRootFormDriven:
    """盘符根判定形态驱动（漏判洞回归锚：posix 运行时形态同样拒）。"""

    @pytest.mark.parametrize(
        "workspace",
        ["C:\\", "C:/", "D:", r"c:\\"],
        ids=["backslash_root", "slash_root", "bare_drive", "lowercase_root"],
    )
    def test_task_submit_rejects_drive_root_on_posix_runtime(
        self, monkeypatch: pytest.MonkeyPatch, workspace: str
    ) -> None:
        """posix 运行时形态下 Windows 盘符根必须拒（收敛前 os.name 门漏判）。

        模拟口径：patch ``os.name``（posix 运行时下为 no-op，Windows 上实证
        py3.12/3.14 对 PosixPath 构造不再抛 NotImplementedError，故无需垫片）；
        ``os.path`` 绑定不受 patch 影响，仍按当前平台归一——形态判定不依赖
        平台，测试在双端语义一致。
        """
        monkeypatch.setattr(os, "name", "posix")
        err = _task_submit._validate_workspace_path(workspace)
        assert err is not None
        assert "根目录" in err

    def test_task_submit_allows_subdir_on_posix_runtime(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """对照：同盘符具体子目录放行（防一刀切拒绝盘符形态的过杀回归）。"""
        monkeypatch.setattr(os, "name", "posix")
        assert _task_submit._validate_workspace_path("C:/work/projects") is None


class TestDangerListSingleSource:
    """危险清单单源：字面锚 + 装配语义 + 两消费方行为一致。"""

    def test_single_source_lists_verbatim(self) -> None:
        """单源两清单与既定字面值逐字一致（原两消费方逐字双份的锚）。"""
        assert _dt.DANGEROUS_WINDOWS_DIRS == _LITERAL_WINDOWS_DIRS
        assert _dt.DANGEROUS_UNIX_DIRS == _LITERAL_UNIX_DIRS

    def test_assembled_set_normpath_lower(self) -> None:
        """装配语义保持：normpath + lower 归一（原消费方循环装配同款）。"""
        expected = {
            os.path.normpath(d).lower()
            for d in _LITERAL_WINDOWS_DIRS + _LITERAL_UNIX_DIRS
        }
        assert expected == _dt.DANGEROUS_DIRS

    @pytest.mark.parametrize("danger", _LITERAL_WINDOWS_DIRS + _LITERAL_UNIX_DIRS)
    def test_both_consumers_reject_each_entry(self, danger: str) -> None:
        """两消费方对清单逐目录拒绝一致（单源后防漂移的行为面锚）。"""
        assert _ISOLATION_WS.validate_workspace_path(danger) is not None
        assert _task_submit._validate_workspace_path(danger) is not None
