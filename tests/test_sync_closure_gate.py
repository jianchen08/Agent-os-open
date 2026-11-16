# @feature: FP-0.2.〇 插件生态 | @ci: python-test
"""依赖闭包同步闸测试：单插件同步撞上跨插件版本错位的拦截面。

2026-10-17 装机事故形态：task_submit v1.1.3 ``from _root_api import
WORKSPACE_PREPARE_PHASE`` × _root_api 未随同更新（生效副本旧版无符号）→
生产 ImportError 合宿组全灭。闸在同步时复现生产解析（BuiltinFirst 选生效
副本 → 真实 import → hasattr 逐符号验证），fail-closed。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location(
    "sync_installed_user_space", _REPO / "scripts" / "sync_installed_user_space.py"
)
assert _SPEC is not None  # 同仓固定路径，装配必成功
assert _SPEC.loader is not None
_sync = importlib.util.module_from_spec(_SPEC)
sys.modules["sync_installed_user_space"] = _sync
_SPEC.loader.exec_module(_sync)

check_import_closure = _sync.check_import_closure

pytestmark = pytest.mark.unit


def _make_install_root(root: Path, dep_version: str, symbols: list[str]) -> Path:
    """构造 <root>/resources/plugins/shared/dep_api/ 内置副本。"""
    dep = root / "resources" / "plugins" / "shared" / "dep_api"
    dep.mkdir(parents=True)
    (dep / "plugin.json").write_text(
        json.dumps({"id": "dep_api", "version": dep_version}), encoding="utf-8"
    )
    body = "\n".join(f"{sym} = {i!r}" for i, sym in enumerate(symbols))
    (dep / "__init__.py").write_text(body + "\n", encoding="utf-8")
    return root


def _make_src(root: Path, server_body: str) -> Path:
    """构造待同步插件 src（server.py 内含跨插件 from-import）。"""
    src = root / "src_plugin"
    src.mkdir(parents=True)
    (src / "plugin.json").write_text(
        json.dumps({"id": "src_plugin", "version": "2.0.0"}), encoding="utf-8"
    )
    (src / "server.py").write_text(server_body, encoding="utf-8")
    return src


def _user_dep(user_plugins: Path, dep_version: str, symbols: list[str]) -> Path:
    dep = user_plugins / "dep_api"
    dep.mkdir(parents=True)
    (dep / "plugin.json").write_text(
        json.dumps({"id": "dep_api", "version": dep_version}), encoding="utf-8"
    )
    body = "\n".join(f"{sym} = {i!r}" for i, sym in enumerate(symbols))
    (dep / "__init__.py").write_text(body + "\n", encoding="utf-8")
    return dep


def test_missing_symbol_detected(tmp_path: Path) -> None:
    """事故形态：生效副本缺被引符号 → 闸红并指认缺哪个。"""
    builtin_root = _make_install_root(tmp_path / "install", "1.0.0", ["A"])
    src = _make_src(tmp_path, "from dep_api import A, B\n")
    user_plugins = tmp_path / "user" / "plugins"
    user_plugins.mkdir(parents=True)

    reason = check_import_closure(src, user_plugins, install_roots=[builtin_root])

    assert reason is not None
    assert "缺符号" in reason
    assert "B" in reason
    assert "dep_api" in reason


def test_closure_ok_when_all_symbols_present(tmp_path: Path) -> None:
    """生效副本符号齐全 → 闸绿。"""
    builtin_root = _make_install_root(tmp_path / "install", "1.0.0", ["A", "B"])
    src = _make_src(tmp_path, "from dep_api import A, B\n")
    user_plugins = tmp_path / "user" / "plugins"
    user_plugins.mkdir(parents=True)

    assert check_import_closure(src, user_plugins, install_roots=[builtin_root]) is None


def test_user_newer_dep_copy_wins_and_supplies_symbol(tmp_path: Path) -> None:
    """用户副本严格更大且补齐符号 → 用户副本为生效副本，闸绿（防拟合对照：
    用户持平内置时内置赢、闸应红）。"""
    builtin_root = _make_install_root(tmp_path / "install", "1.0.0", ["A"])
    src = _make_src(tmp_path, "from dep_api import A, B\n")
    user_plugins = tmp_path / "user" / "plugins"
    user_plugins.mkdir(parents=True)
    _user_dep(user_plugins, "1.1.0", ["A", "B"])

    assert check_import_closure(src, user_plugins, install_roots=[builtin_root]) is None


def test_user_equal_version_builtin_wins_gate_red(tmp_path: Path) -> None:
    """用户副本持平内置（1.0.0）→ BuiltinFirst 内置赢（无 B）→ 闸红。"""
    builtin_root = _make_install_root(tmp_path / "install", "1.0.0", ["A"])
    src = _make_src(tmp_path, "from dep_api import A, B\n")
    user_plugins = tmp_path / "user" / "plugins"
    user_plugins.mkdir(parents=True)
    _user_dep(user_plugins, "1.0.0", ["A", "B"])

    reason = check_import_closure(src, user_plugins, install_roots=[builtin_root])

    assert reason is not None
    assert "缺符号" in reason
    assert "B" in reason


def test_dep_missing_everywhere(tmp_path: Path) -> None:
    """依赖插件双侧都不存在 → 闸红指认。"""
    src = _make_src(tmp_path, "from ghost_api import X\n")
    user_plugins = tmp_path / "user" / "plugins"
    user_plugins.mkdir(parents=True)

    reason = check_import_closure(src, user_plugins, install_roots=[tmp_path / "absent"])

    assert reason is not None
    assert "ghost_api" in reason
    assert "均未找到" in reason


def test_future_import_not_treated_as_dependency(tmp_path: Path) -> None:
    """`from __future__ import annotations` 是编译器指令非跨插件依赖——
    server.py 常备该导入，误收 = 任何插件 sync 恒报闭包不满足（2026-10-17
    mode_roleplay sync 实证）。豁免后同形态应绿。"""
    dep = tmp_path / "plugins" / "shared" / "dep_plug"
    dep.mkdir(parents=True)
    (dep / "plugin.json").write_text('{"version": "1.0.0"}', encoding="utf-8")
    src = tmp_path / "plugins" / "shared" / "src_plug"
    src.mkdir(parents=True)
    (src / "plugin.json").write_text('{"version": "1.0.0"}', encoding="utf-8")
    (src / "server.py").write_text(
        "from __future__ import annotations\n\nx = 1\n",
        encoding="utf-8",
    )
    gate = check_import_closure
    result = gate(src, tmp_path / "user_plugins")
    assert result is None, f"__future__ 不应记为跨插件依赖：{result}"


def test_no_cross_plugin_imports_is_green(tmp_path: Path) -> None:
    """无跨插件 from-import → 闸不做任何判定直接绿（插件内引用不归本闸）。"""
    src = _make_src(tmp_path, "import json\nfrom . import helper\nVALUE = 1\n")

    assert (
        check_import_closure(src, tmp_path / "user" / "plugins", install_roots=[])
        is None
    )


def test_dep_import_failure_reported(tmp_path: Path) -> None:
    """生效副本自身导入即炸（事故同款 ImportError 形态）→ 闸红带根因。"""
    builtin_root = _make_install_root(tmp_path / "install", "1.0.0", ["A"])
    dep = builtin_root / "resources" / "plugins" / "shared" / "dep_api"
    (dep / "__init__.py").write_text(
        "raise ModuleNotFoundError(\"No module named 'kernel_db'\")\n", encoding="utf-8"
    )
    src = _make_src(tmp_path, "from dep_api import A\n")
    user_plugins = tmp_path / "user" / "plugins"
    user_plugins.mkdir(parents=True)

    reason = check_import_closure(src, user_plugins, install_roots=[builtin_root])

    assert reason is not None
    assert "导入失败" in reason
    assert "kernel_db" in reason


@pytest.mark.skipif(
    sys.platform != "win32",
    reason="闸在 Windows 生产机执行；非 Windows 仅保语义回归",
)
def test_probe_uses_real_interpreter_semantics(tmp_path: Path) -> None:
    """探针跑真实解释器：符号经模块真实解析（含 PEP 562 动态转发）而非静态表。"""
    builtin_root = _make_install_root(tmp_path / "install", "1.0.0", [])
    dep = builtin_root / "resources" / "plugins" / "shared" / "dep_api"
    (dep / "__init__.py").write_text(
        "_EXPORTS = {'DYN': 1}\n"
        "def __getattr__(name):\n"
        "    if name in _EXPORTS:\n"
        "        return _EXPORTS[name]\n"
        "    raise AttributeError(name)\n",
        encoding="utf-8",
    )
    src = _make_src(tmp_path, "from dep_api import DYN\n")
    user_plugins = tmp_path / "user" / "plugins"
    user_plugins.mkdir(parents=True)

    assert check_import_closure(src, user_plugins, install_roots=[builtin_root]) is None
