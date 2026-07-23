"""LLM 插件配置注入桥（task_11 P0-3）。

本模块是 sidecar 内部的配置注入桥，供三处复用：
- ``server.py::_on_load`` 调 ``set_config(config)`` 注入内核下发的配置；
- ``router_factory._ensure_provider_type_map_loaded`` 和
  ``adapter.KeyPoolAdapter._route_call`` 调 ``get_model_config_loader()``
  拿到一个与 0.1 ``ModelConfigLoader`` 接口兼容的 loader（暴露 ``_load_llm_data``）。

配置结构（P1：manifest ``config_files`` 映射，按 id 命名空间合并后注入）::

    {"llm": <llm.yaml 内容>, "embedding": <embedding.yaml 内容>}

``_load_llm_data`` 返回 ``config["llm"]``，即 ``llm.yaml`` 的完整内容
（含顶层 ``models`` / ``providers`` / ``defaults`` / ``concurrency`` 键）。

[来源: docs/tasks/task_11_plugin_capability_unification.md P1-3；ADR §4.3 B3]
"""
from __future__ import annotations

from typing import Any

# 模块级配置存储（由 set_config 注入，进程内单例）
_config: dict[str, Any] = {}


def set_config(config: dict[str, Any]) -> None:
    """注入内核下发的插件配置。

    Args:
        config: 内核经 ``config_files`` 映射合并后注入的配置字典（P1 起）。
    """
    global _config  # noqa: PLW0603
    _config = config if isinstance(config, dict) else {}


def get_config() -> dict[str, Any]:
    """获取当前注入的配置（调试用）。"""
    return _config


def get_model_config_loader() -> ModelConfigLoaderShim:
    """返回一个与 0.1 ``ModelConfigLoader`` 接口兼容的 loader 实例。

    loader 暴露 ``_load_llm_data()``，返回当前注入配置中的 ``llm`` 命名空间节。
    """
    return ModelConfigLoaderShim(_config)


class ModelConfigLoaderShim:
    """模拟 0.1 ``ModelConfigLoader`` 接口，数据来自注入配置。

    ``router_factory.build_router`` / ``build_adapter`` 及
    ``adapter.KeyPoolAdapter._route_call`` 调用 ``_load_llm_data()``
    获取 ``llm.yaml`` 解析后的字典。
    """

    def __init__(self, config: dict[str, Any]) -> None:
        self._config = config

    def _load_llm_data(self) -> dict[str, Any]:
        """返回 ``llm.yaml`` 内容（即注入配置的 ``llm`` 命名空间节）。

        P1 config_files 映射后内核注入结构为
        ``{"llm": <llm.yaml 全文>, "embedding": <embedding.yaml 全文>}``，
        本方法取 ``config["llm"]``。缺失或非 dict 返回 ``{}``，
        不抛异常（让调用方按空配置降级，而非崩溃）。
        """
        llm_config = self._config.get("llm")
        if not isinstance(llm_config, dict):
            return {}
        return llm_config
