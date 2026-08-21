# @feature: FP-0.2.一 第三方插件协议·python_packager 装载 | @ci: python-coverage
"""python_packager 装载插件测试（uv 后端）：真装真卸 + fail-closed 负例。

覆盖：
1. install：uv sync 建 .venv + 写 uv.lock（哈希完整锁定）
2. uninstall：删 .venv + uv.lock（可反复卸载不炸）
3. resolve_dependencies：未装=缺（不假装绿）；装后=全满足；非 uv 包=报错
4. status：uv 版本 / venv / lock / 声明依赖
5. 负例：非 uv 包目录 install/resolve/status → {ok:false}
"""

from __future__ import annotations

import asyncio
import importlib.util
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))


@pytest.fixture(autouse=True)
def _whitelist_pytest_temp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """边界加固（2026-08-19 A-5）后：本批用例的包目录全在 pytest 临时根下，
    经 PYTHON_PACKAGER_ALLOWED_DIRS env 通道显式白名单（临时区非安全边界，
    其余用例按需 monkeypatch delenv 越过该白名单验证越界拒绝）。"""
    monkeypatch.setenv("PYTHON_PACKAGER_ALLOWED_DIRS", str(tmp_path.parent))

# SDK 路径（agentos_plugin_sdk 未 pip 安装时）：python_packager 位于
# plugins/shared/system/python_packager → parents[3] = plugins → plugins/sdk/src
_SDK_DIR = Path(__file__).resolve().parents[3] / "sdk" / "src"
if str(_SDK_DIR) not in sys.path:
    sys.path.insert(0, str(_SDK_DIR))


def _load_server() -> Any:
    mod_name = "python_packager_server_test"
    module_path = _PLUGIN_DIR / "server.py"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, module_path)
    assert spec is not None and spec.loader is not None, "Cannot load server.py"
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


SERVER = _load_server()

_UV_AVAILABLE = shutil.which(SERVER._uv()) is not None


def _make_uv_package(tmp_path: Path, deps: list[str] | None = None) -> Path:
    """构造最小 uv 包：pyproject.toml（无三方依赖=可离线解析）。"""
    d = tmp_path / "pkg"
    d.mkdir()
    dep_lines = "".join(f'        "{x}",\n' for x in (deps or []))
    (d / "pyproject.toml").write_text(
        f"[project]\n"
        f'name = "pkg"\n'
        f'version = "1.0.0"\n'
        f'requires-python = ">=3.11"\n'
        f"dependencies = [\n{dep_lines}]\n",
        encoding="utf-8",
    )
    return d


def _run(coro) -> Any:
    """同步包装 async 工具函数（pytest-asyncio auto 之外的确定性执行）。"""
    return asyncio.run(coro)


@pytest.mark.skipif(
    not _UV_AVAILABLE, reason="uv 不在 PATH（本插件后端依赖外部成熟包管理器 uv）"
)
class TestInstall:
    async def test_install_creates_venv_and_lock(self, tmp_path):
        d = _make_uv_package(tmp_path)
        res = await SERVER.install(str(d))
        assert res["ok"], res
        assert res["venv_exists"], "uv sync 应建 .venv"
        assert res["lock_present"], "uv sync 应写 uv.lock（哈希完整锁定）"
        assert res["declared_dependencies"] == []

    async def test_install_reports_bad_uv(self, tmp_path, monkeypatch):
        d = _make_uv_package(tmp_path, deps=["nothing-real"])
        monkeypatch.setenv("PYTHON_PACKAGER_UV", "definitely-not-uv-exe")
        res = await SERVER.install(str(d))
        assert not res["ok"]
        assert "找不到可执行文件" in res["error"] or "uv 不可用" in res["error"], res


@pytest.mark.skipif(
    not _UV_AVAILABLE, reason="uv 不在 PATH（本插件后端依赖外部成熟包管理器 uv）"
)
class TestUninstall:
    async def test_uninstall_removes_venv_and_lock(self, tmp_path):
        d = _make_uv_package(tmp_path)
        assert (await SERVER.install(str(d)))["ok"]
        res = await SERVER.uninstall(str(d))
        assert res["ok"] and len(res["removed"]) >= 1
        assert not res["venv_exists"] and not res["lock_exists"]
        # 可重复卸载（幂等）
        res2 = await SERVER.uninstall(str(d))
        assert res2["ok"] and res2["removed"] == []


@pytest.mark.skipif(
    not _UV_AVAILABLE, reason="uv 不在 PATH（本插件后端依赖外部成熟包管理器 uv）"
)
class TestResolveDependencies:
    async def test_uninstalled_reports_missing_not_green(self, tmp_path):
        d = _make_uv_package(tmp_path, deps=["requests>=2.0"])
        res = await SERVER.resolve_dependencies(str(d))
        assert res["ok"] and res["resolved"] is False
        assert res["satisfied"] == []
        assert "requests" in "\n".join(res["missing"]), "未装=诚实标缺（不假装绿）"

    async def test_installed_reports_satisfied(self, tmp_path):
        d = _make_uv_package(tmp_path)
        assert (await SERVER.install(str(d)))["ok"]
        res = await SERVER.resolve_dependencies(str(d))
        assert res["ok"] and res["resolved"] is True
        assert res["missing"] == []


async def test_status_reports_uv_and_flags(tmp_path):
    d = _make_uv_package(tmp_path, deps=["requests>=2.0"])
    res = await SERVER.status(str(d))
    assert res["ok"]
    if _UV_AVAILABLE:
        assert res["uv_version"], "uv 可用时应报版本"
    assert res["venv_exists"] is False and res["lock_exists"] is False
    assert any("requests" in x for x in res["declared_dependencies"])


async def test_not_a_uv_package_fail_closed(tmp_path):
    d = tmp_path / "notapkg"
    d.mkdir()
    # 安装/解析/状态：非 uv 包目录 → fail-closed 明确报错。
    for fn in (SERVER.install, SERVER.resolve_dependencies, SERVER.status):
        res = await fn(str(d))
        assert not res["ok"], f"{fn.__name__} 对非 uv 包目录应 fail-closed"
        assert "不是 uv 包" in res["error"], res
    # 卸载：本来就是清理环境产物的空操作——非 uv 包目录也应幂等成功（无物可删）。
    res = await SERVER.uninstall(str(d))
    assert res["ok"] and res["removed"] == [], res


# ── 包目录边界（安全审查 2026-08-19 A-5）──────────────────────────────
class TestPackageDirBoundary:
    """packaging.python.* 只允许 plugins/** 或 PYTHON_PACKAGER_ALLOWED_DIRS 白名单。

    前提：本插件（语言域装载）的服务语义就是装"插件"包，plugins/ 即其边界；
    越界（任意绝对目录）意味着让 uv 按攻击者构造的 pyproject 安装依赖=任意代码执行。
    """

    async def test_plugins_dir_allowed_without_env(self, monkeypatch) -> None:
        monkeypatch.delenv("PYTHON_PACKAGER_ALLOWED_DIRS", raising=False)
        # server.py 自身目录在 plugins/shared/system/python_packager → 边界内放行。
        # 批 C venv 化（1b31d7ab）后本插件自身即是合法 uv 包（pyproject+uv.lock
        # 在位），status 应成功；无论环境差异，错误绝不能是"不在允许范围"（越界）。
        res = await SERVER.status(str(_PLUGIN_DIR))
        assert "不在允许范围" not in (res.get("error") or ""), res

    async def test_outside_plugins_denied(self, tmp_path, monkeypatch) -> None:
        monkeypatch.delenv("PYTHON_PACKAGER_ALLOWED_DIRS", raising=False)
        d = _make_uv_package(tmp_path)  # 真实 uv 包，但越界 → 必须在 uv 动作前被拒
        for fn in (SERVER.install, SERVER.resolve_dependencies, SERVER.status):
            res = await fn(str(d))
            assert not res["ok"], f"{fn.__name__} 越界目录应 fail-closed"
            assert "不在允许范围" in res["error"], res
        assert not (d / ".venv").exists(), "越界时不得执行任何 uv 动作（无副作用）"
        assert not (d / "uv.lock").exists()

    async def test_outside_uninstall_denied(self, tmp_path, monkeypatch) -> None:
        monkeypatch.delenv("PYTHON_PACKAGER_ALLOWED_DIRS", raising=False)
        res = await SERVER.uninstall(str(tmp_path))
        assert not res["ok"] and "不在允许范围" in res["error"], res

    async def test_env_whitelist_escape_hatch(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("PYTHON_PACKAGER_ALLOWED_DIRS", str(tmp_path))
        res = await SERVER.status(str(tmp_path))
        assert not res["ok"]
        assert "不在允许范围" not in res["error"], res  # 放行走 uv 包校验而非越界拒绝

    async def test_illegal_path_string_rejected(self, monkeypatch) -> None:
        res = await SERVER.install("\x00bad")
        assert not res["ok"], res
