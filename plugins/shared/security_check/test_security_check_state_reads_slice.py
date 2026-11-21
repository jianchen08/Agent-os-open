# @feature: FP-0.2.〇 管道引擎与插件执行模型（位置闸管道层执法） | @ci: python-coverage
"""security_check `state.reads` 切片投喂回归（授权卡发起人归属四元组）。

漏声明形态（2026-11-12 修复）：`user_id` 与 `context.agent_name` 未进 manifest
`state.reads` → 切片后恒读不到 → 位置闸授权卡（`create_choice`）的
`user_id` 恒空串、`agent_name` 恒空串：卡片落不到发起者、多 agent 并行时
用户无法从卡面分辨谁在申请（同族 agent_id/agent_level/pipeline_id 三项已声明，
只剩这两项在切片下静默缺失，症状不对称更难发现）。

本用例按 manifest 声明切片投喂（与生产 invoker 同形）断言两键到达卡片载荷。
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_DIR = Path(__file__).resolve().parent
_SHARED = _DIR.parents[0]  # plugins/shared/

for _d in (str(_DIR), str(_SHARED)):
    if _d not in sys.path:
        sys.path.insert(0, _d)

import repo_anchor  # noqa: E402
from _libs.testing.state_slice import (  # noqa: E402
    manifest_reads,
    slice_feed,
    visible_keys,
)


def _load_plugin_module() -> Any:
    mod_name = "security_check_state_reads_slice_test"
    sys.modules.pop(mod_name, None)
    spec = importlib.util.spec_from_file_location(mod_name, _DIR / "plugin.py")
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


MANIFEST_READS = manifest_reads(_DIR)


class _RecordingHICap:
    """human-interaction 假 capability：记录卡片载荷并批「仅本管道」。"""

    def __init__(self) -> None:
        self.cards: list[dict[str, Any]] = []

    async def call(self, method: str, params: dict[str, Any], **_kwargs: Any) -> Any:
        if method == "create_choice":
            self.cards.append(dict(params))
            return {"request_id": "req-1"}
        if method == "wait_for_choice":
            return {
                "selected_option": "pipeline",
                "selected_semantics": "grant_write",
            }
        raise AssertionError(f"unexpected method {method}")


@pytest.fixture
def zone_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Path]]:
    """假仓库锚 + tmp 名单 + workspace 布局（与 test_zone_gate.py 同款）。"""
    repo = tmp_path / "repo"
    (repo / "config" / "kernel").mkdir(parents=True)
    (repo / "config" / "llm.yaml").write_text("api_key: x\n", encoding="utf-8")
    ws = tmp_path / "sessions" / "s1"
    ws.mkdir(parents=True)
    users_dir = tmp_path / "config" / "users"
    (users_dir / "default").mkdir(parents=True)
    monkeypatch.setenv("AGENTOS_CONFIG_ROOT", str(repo / "config"))
    monkeypatch.setenv("AGENTOS_CONFIG_USERS_DIR", str(users_dir))
    monkeypatch.setenv("AGENTOS_USER_ROOT", str(tmp_path / "usroot"))
    repo_anchor.reset_cache()
    yield {"repo": repo, "ws": ws, "users_dir": users_dir, "tmp": tmp_path}
    repo_anchor.reset_cache()


def _ask_for_write(zone_env: dict[str, Path], **state_overrides: Any) -> dict[str, Any]:
    """造一次写区外写操作 → 切片投喂 → 跑位置闸 → 返回卡片载荷。"""
    mod = _load_plugin_module()
    cap = _RecordingHICap()
    mod.set_human_interaction_cap(cap)
    outsider = zone_env["tmp"] / "denied_dir"
    outsider.mkdir(exist_ok=True)
    full_state: dict[str, Any] = {
        "core_type": "tool_execute",
        "workspace": str(zone_env["ws"]),
        "project_root": "",
        "session_id": "sess-zone",
        "pipeline_id": "p-zone",
        "user_id": "u-42",
        "context.agent_name": "code_writer_agent",
        "raw_tool_calls": [
            {"name": "file_write", "args": {"path": str(outsider / "a.txt")}, "id": "c-1"}
        ],
        "messages": [],
    }
    full_state.update(state_overrides)
    fed = slice_feed(full_state, MANIFEST_READS)

    def _get_service(name: str) -> Any:
        raise KeyError(name)

    ctx = types.SimpleNamespace(state=fed, get_service=_get_service)
    updates: dict[str, Any] = {}
    asyncio.run(mod.SecurityCheckPlugin({})._enforce_zone_policy(ctx, fed["raw_tool_calls"], updates))
    assert cap.cards, "写区外必须弹授权卡"
    return cap.cards[0]


class TestDeclaration:
    def test_slice_carried_keys_declared(self) -> None:
        declared = visible_keys(MANIFEST_READS)
        for key in ("user_id", "context.agent_name"):
            assert key in declared, f"{key} 未在 state.reads 声明 → 授权卡恒缺发起人"


class TestApprovalCardAttributionUnderSliceFeed:
    def test_card_carries_user_and_agent_name_under_slice_feed(
        self, zone_env: dict[str, Path]
    ) -> None:
        card = _ask_for_write(zone_env)

        assert card["user_id"] == "u-42", f"卡片 user_id 缺失（切片读点漏声明）：{card}"
        assert card["agent_name"] == "code_writer_agent", (
            f"卡片 agent_name 缺失（切片读点漏声明）：{card}"
        )

    def test_card_omits_empty_attribution_gracefully(
        self, zone_env: dict[str, Path]
    ) -> None:
        """无 user_id / agent_name 的管道（系统侧管道）照常弹卡，值为空串不炸。"""
        card = _ask_for_write(zone_env, **{"user_id": "", "context.agent_name": ""})

        assert card["user_id"] == ""
        assert card["agent_name"] == ""
        assert card["title"].startswith("授权写入")
