"""mode_tests 共享 patch 传播夹具（mode_roleplay 拆分连带修复）。

e2ab643d1 把 mode_roleplay server.py 按域拆成 rp_common/rp_cards/rp_content/rp_game
后，各子模块持有 from-import 进自己命名空间的绑定；只 patch server 模块属性
不可达 rp_* 内的消费点。本夹具把 patch 传播到全部已装载且持有该符号的
rp_* 命名空间（monkeypatch 自动还原，防共享命名空间跨测试泄漏），
语义与拆分前「单模块全局 patch」等价——task_submit 拆分先例同款口径：
monkeypatch 目标随拆分命名空间修正。
"""
from __future__ import annotations

import sys
from typing import Any

import pytest

_RP_MODULES = ("rp_common", "rp_cards", "rp_content", "rp_game")


@pytest.fixture
def patch_shared(monkeypatch: pytest.MonkeyPatch):
    """返回传播函数：把 name=value 写入所有持有该符号的 rp_* 命名空间。

    只碰已装载（sys.modules 在位）且确实持有该属性的模块；返回命中模块名
    列表（空 = 符号未被任何 rp_* 模块持有，调用方可据此断言）。
    """

    def _patch(name: str, value: Any) -> list[str]:
        hits: list[str] = []
        for modname in _RP_MODULES:
            mod = sys.modules.get(modname)
            if mod is not None and hasattr(mod, name):
                monkeypatch.setattr(mod, name, value)
                hits.append(modname)
        return hits

    return _patch
