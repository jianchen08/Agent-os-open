"""card 质检服务测试替身（T6c 共享测试基建）。

以真实 mode_roleplay card_lint 纯函数为后端的能力缝桩：关键路径走真实依赖
（仅通道换桩），wire 契约与生产 provides 直连 1:1（方法短名 + 裸 dict 返回，
无信封）。供 agent_config_load（lint_decl/lint_actions/lint_pack_ui）与
mode_evolution content_audit（lint_extensions/scan_*/texts_of_tree）车道用；
fail-closed 语义测试用 ``None`` 注入（服务缺席）自设，不在本替身范围。

用法（车道需把 plugins/shared 置于 sys.path——_libs 包路径；card_lint 宿主
目录由替身自推）::

    from _libs.testing.card_service_fake import card_service_fake, install_card_service_fake
    install_card_service_fake(plugin_instance)            # agent_config_load 实例缝
    proposal.set_card_service_caller(card_service_fake)   # mode_evolution 模块缝
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

# card_lint 单源宿主（mode_roleplay 插件目录）由替身自推——车道侧无需记路径面。
# 无条件幂等插入（sys.path 重复项对 import 无害；条件分支会让该行随车道先后
# 顺序在 diff coverage 下漂红）。
_HOST_DIR = Path(__file__).resolve().parents[2] / "mode_roleplay"
sys.path.insert(0, str(_HOST_DIR))

from card_lint import (  # noqa: E402 —— 宿主目录自推后导入（车道 sys.path 面见模块 docstring）
    lint_actions,
    lint_behaviors,
    lint_card_extensions,
    lint_engine,
    lint_init_ui,
    lint_ui,
    lint_variables,
    scan_html_danger,
    scan_html_danger_texts,
    scan_suspicious,
    walk_str_values,
)


async def card_service_fake(method: str, params: dict[str, Any]) -> dict[str, Any]:
    """真实 card_lint 后端的服务桩（生产 wire 契约：方法短名 + 裸 dict 返回）。"""
    if method == "lint_decl":
        warnings: list[str] = []
        if params.get("variables") is not None:
            warnings.extend(lint_variables(params["variables"]))
        if params.get("behaviors") is not None:
            warnings.extend(lint_behaviors(params["behaviors"]))
        if params.get("engine") is not None:
            warnings.extend(lint_engine(params["engine"]))
        return {"warnings": warnings}
    if method == "lint_actions":
        valid, action_warnings = lint_actions(params.get("actions"))
        return {"actions": valid, "warnings": action_warnings}
    if method == "lint_pack_ui":
        return {"warnings": [*lint_ui(params.get("ui")), *lint_init_ui(params.get("init_ui"))]}
    if method == "lint_extensions":
        return {"warnings": lint_card_extensions(params.get("extensions"))}
    if method == "scan_suspicious":
        return {"warnings": scan_suspicious(params["text"], params.get("subject") or "角色设定")}
    if method == "scan_html_card":
        return {"warnings": scan_html_danger(params["card"])}
    if method == "scan_html_texts":
        return {"warnings": scan_html_danger_texts(params["texts"], params.get("subject") or "角色卡内容")}
    if method == "texts_of_tree":
        return {"texts": walk_str_values(params["data"])}
    raise KeyError(method)


def install_card_service_fake(plugin: Any) -> None:
    """把替身装上 agent_config_load 实例缝（set_card_service_caller）。"""
    plugin.set_card_service_caller(card_service_fake)
