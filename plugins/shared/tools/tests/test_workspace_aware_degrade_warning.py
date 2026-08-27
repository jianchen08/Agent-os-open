# @feature: FP-0.2.二 工具路径校验降级可观测（兜底反模式审查 P7） | @ci: none-local
"""workspace_aware 路径权限校验层不可用时 fail-closed 契约测试。

锁两件事（scan批B 替代原"降级放行"契约）：
1. 策略层不可用时拒绝所有路径操作并给出可传播给用户的拒绝原因——
   安全控制的失效模式是拒绝而不是放行；
2. 不可用检测必须一次性 warning 留痕（多层防线同时哑火时必须可观测），
   后续拒绝不重复刷屏但照常执行。

[来源: docs/working/规则驱动全仓扫描报告_20260827.md tools Must Fix #2]
"""

from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_TOOLS_DIR = Path(__file__).resolve().parent.parent  # plugins/shared/tools/
_DOWNLOAD_DIR = _TOOLS_DIR / "download"
# 平铺插件目录自治：自带策略模块与其消费方（workspace_aware）都在插件目录，
# 由本测试显式挂载——host 端等价物是 server.py 的 sys.path 引导。
if str(_DOWNLOAD_DIR) not in sys.path:
    sys.path.insert(0, str(_DOWNLOAD_DIR))


def _load_module():
    mod_name = "workspace_aware_under_test"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, _TOOLS_DIR / "download" / "workspace_aware.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


class _Tool:
    """最小宿主：混入 WorkspaceAwareMixin 并预置 workspace/project_root。"""

    def __init__(self, workspace_aware_mod):
        # 动态混入（避免模块加载顺序依赖）
        self.__class__ = type(
            "_ToolWithMixin", (workspace_aware_mod.WorkspaceAwareMixin, _Tool), {}
        )
        self._workspace = Path("D:/ws")
        self._project_root = Path("D:/proj")


@pytest.fixture
def fresh_tool(monkeypatch):
    """全新 mixin 实例：清类级策略管理器缓存与一次性告警标记。"""
    mod = _load_module()
    monkeypatch.setattr(mod.WorkspaceAwareMixin, "_policy_manager", None)
    monkeypatch.setattr(mod, "_policy_unavailable_warned", False)
    return mod, _Tool(mod)


def test_policy_layer_unavailable_fails_closed(fresh_tool, monkeypatch):
    """策略管理器不可加载 → 读写全部拒绝；拒绝原因可传播给用户。"""
    mod, tool = fresh_tool
    # 生产中任何加载失败最终都落在"manager 为 None"这一状态；
    # 经此接缝注入该状态以锁定决策逻辑（模块系统占位法不可靠且越权）。
    monkeypatch.setattr(
        mod.WorkspaceAwareMixin,
        "_get_policy_manager",
        classmethod(lambda _cls: None),
    )

    ok_write, err_write = tool.check_path_allowed("outside/file.txt", "write", 2)
    ok_read, err_read = tool.check_path_allowed("outside/file.txt", "read", 1)

    assert not ok_write, "fail-closed：策略层缺失时写入拒绝"
    assert not ok_read, "fail-closed：策略层缺失时读取同样拒绝"
    for err in (err_write, err_read):
        assert err, "拒绝必须带原因"
        assert "拒绝" in err
        assert "权限" in err


def test_failed_load_not_cached(fresh_tool):
    """不可用期间多次调用后类级缓存仍为空（加载失败的结果不得被当作成功缓存）。"""
    mod, tool = fresh_tool
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        mod.WorkspaceAwareMixin,
        "_get_policy_manager",
        classmethod(lambda _cls: None),
    )
    try:
        tool.check_path_allowed("a.txt", "write", 1)
        tool.check_path_allowed("b.txt", "read", 2)
        assert mod.WorkspaceAwareMixin._policy_manager is None
    finally:
        monkeypatch.undo()


def test_checker_failure_denies_and_warns_once(monkeypatch, caplog):
    """检查器执行异常 → 拒绝 + 恰好一条 warning；重复调用拒绝但不重复刷屏。"""
    mod = _load_module()
    monkeypatch.setattr(mod.WorkspaceAwareMixin, "_policy_manager", None)
    monkeypatch.setattr(mod, "_policy_unavailable_warned", False)
    # 让已真实导入的镜像 permission_checker 抛错：接缝是模块属性，
    # 不 mock 内部方法、不改实现文件。
    import permission_checker as _pc_mod

    def _boom(*_a, **_k):
        raise RuntimeError("checker unavailable")

    monkeypatch.setattr(_pc_mod, "PermissionChecker", _boom)
    tool = _Tool(mod)

    def _own_warns() -> int:
        return len(
            [r for r in caplog.records if r.levelno >= logging.WARNING and r.name.startswith("workspace_aware")]
        )

    with caplog.at_level(logging.WARNING):
        ok1, err1 = tool.check_path_allowed("a.txt", "write", 1)
        warns_first = _own_warns()
        ok2, err2 = tool.check_path_allowed("b.txt", "write", 2)
    warns_total = _own_warns()

    assert not ok1, "首次调用拒绝"
    assert not ok2, "每次调用都维持 fail-closed"
    assert "拒绝" in err1
    assert warns_first == 1, "首次不可用必须留一条 warning"
    assert warns_total == 1, "一次性告警：后续拒绝不重复刷屏"


def test_uninitialized_workspace_still_rejected():
    """workspace 未初始化 → 显式拒绝（回归：fail-closed 不等于无条件拒绝）。"""
    mod = _load_module()
    tool = mod.WorkspaceAwareMixin()
    ok, err = tool.check_path_allowed("x.txt", "read", 1)
    assert not ok
    assert "未初始化" in err
