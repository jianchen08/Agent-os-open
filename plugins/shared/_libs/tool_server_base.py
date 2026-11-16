"""工具插件 server.py 共享基面：防遮蔽加载 + 失败信封（审查总册 AR-M3）。

browser / community_ops / ecosystem_import / oauth_broker / search /
web_ext 六件工具插件的
server.py 是同一形态的接口适配层：handler 内显式按文件路径加载本目录
tool.py 的工具类，并把 ToolExecutionResult 归一成对内核/前端可见的信封。
本模块承载两面的唯一实现，六件 server.py 各留薄封装（模块级缓存槽位
与测试缝在 server 侧，加载/信封语义单源在此）。

防遮蔽语义（合宿平铺 sys.path 下裸名槽位被兄弟成员争用）：
- spec 唯一名按文件路径 exec 本目录 tool.py，模块 ``__name__`` 即该唯一名，
  并注册 ``sys.modules[<唯一名>]``（server 侧测试/打桩按名定位 impl 模块）；
- exec 窗口内本目录置顶 sys.path，且摘除静息态被异成员占据的本目录顶层
  模块槽位（sys.modules 命中优先于 sys.path，仅置顶路径挡不住——邻成员
  装载期 import 的同名模块会让 call-time exec 命中异成员，实测命中
  triggers_ext/tool.py 与 cost_control/constants 两族）；
- exec 后按 host loader 同款语义恢复静息态：被摘槽位原样回写，本目录
  置顶路径全部移除（临时提升仅覆盖加载期）。

失败信封契约（ADR 2026-09-28 收编）：成功透传工具 output；失败显式
``{"success": False, "error": ...}``——裸 error 载荷会被内核归一层包成
success 伪信封，LLM/前端把失败当成功消费。
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from typing import Any

__all__ = ["load_tool_class", "tool_result_envelope"]


def load_tool_class(plugin_dir: str, impl_module: str, class_name: str) -> Any:
    """显式按文件路径加载 ``<plugin_dir>/tool.py`` 的工具类（合宿防遮蔽）。

    Args:
        plugin_dir: 本插件目录（server.py 的 ``os.path.dirname(__file__)``）。
        impl_module: impl 模块唯一名（如 ``"browser_tool_impl"``），同时是
            返回类的 ``__module__`` 与 sys.modules 注册键。
        class_name: 工具类名（``getattr`` 取自 exec 后的 impl 模块）。

    Raises:
        ImportError: tool.py 规格解析失败（目录/文件不可达或加载器缺失）。
    """
    here = os.path.abspath(plugin_dir)
    sys.path.insert(0, here)
    masked: dict[str, Any] = {}
    try:
        # 摘除异成员占据的本目录顶层模块槽位（host loader 同款手法）：本目录
        # 提供的顶层 .py 名在 exec 窗口内一律本目录优先，异成员模块 exec 后
        # 原样恢复；本目录来源的驻留模块（重载场景）不动。
        here_prefix = os.path.normcase(here) + os.sep
        for entry in Path(here).iterdir():
            if entry.suffix != ".py":
                continue
            name = entry.stem
            module = sys.modules.get(name)
            file = getattr(module, "__file__", None)
            if module is None or (
                file and os.path.normcase(os.path.abspath(file)).startswith(here_prefix)
            ):
                continue
            masked[name] = sys.modules.pop(name)
        spec = importlib.util.spec_from_file_location(
            impl_module, os.path.join(here, "tool.py")
        )
        if spec is None or spec.loader is None:  # pragma: no cover —— 路径恒为
            # 本目录 tool.py 字面名，None 形态仅 mypy 收窄所需的防御样板
            raise ImportError(f"tool.py 规格解析失败: {os.path.join(here, 'tool.py')}")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[impl_module] = mod
        spec.loader.exec_module(mod)
    finally:
        while here in sys.path:
            sys.path.remove(here)
        for name, module in masked.items():
            sys.modules[name] = module
    return getattr(mod, class_name)


def tool_result_envelope(result: Any, *, status: int | None = None) -> dict[str, Any]:
    """ToolExecutionResult → handler 返回信封。

    成功透传 ``result.output``；失败构造显式 ``success=False`` 信封。
    ``result.error_code`` 非空时随信封携带（错误分类面，如社区工具
    TOKEN_MISSING）；``status`` 供 output_schema required 含 status 的工具
    补 HTTP 语义位（如 browser 的 500）。
    """
    if result.success:
        return result.output
    payload: dict[str, Any] = {"success": False, "error": result.error}
    error_code = getattr(result, "error_code", None)
    if error_code:
        payload["error_code"] = error_code
    if status is not None:
        payload["status"] = status
    return payload
