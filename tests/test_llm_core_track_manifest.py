# @feature: FP-0.2.一 插件协议（H2 契约基建） | @ci: python-coverage
"""llm_core manifest 声明契约（track 并入后的装载面，ADR 2026-09-15-track-merged-into-llm-core）。

并入把 track 的三件职责（token 累计 / 耗时统计 / 两个观察出口）移进 llm_core，
manifest 必须同步承接原 track 声明的全部面——**漏一项都是静默半死**：

1. ``state.reads`` 缺键 → 切片投喂下该键对插件不可见（声明非空即白名单制，
   invoker/src/shared.rs:assemble_state_slice）。并入后需新增：
   ``track.llm_usage``（累计基准，缺则累计退化为每轮覆盖）、
   ``track.messages_chars``（W2a 字符锚——**并入前就缺**，见下）。
2. ``persistent_fields`` / ``export_fields`` 缺键 → 累计值不落库、不出 state
   摘要面（前端与 monitoring/tasks 通知读不到）。``track.execution_stats`` 已
   随并入退役（无生产消费方、历史无业务数据，用户裁定 2026-09-15），两个名单
   均不得再声明它。
3. ``granted_capabilities`` 缺 frontend/metrics → G6 单点授权拒绝
   （capability_router.rs:check_grant 白名单制），两个观察出口恒 403。

已知缺陷（本文件同时是回归闸）：llm_core 自 ed4f69fd2 起读
``track.messages_chars`` 落 W2a 锚，但该键从未进 ``state.reads``（切片投喂
4705d4e12 先落地、reads 收口 c03e3f2dd 在后），实测库内 157 条
``track.messages_chars_at_llm`` 全为 0，而同批 ``track.messages_chars`` 有
3447 条真值——autonomous.yaml 上下文粗门公式的增量支恒退化。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_REPO = Path(__file__).resolve().parent.parent
_MANIFEST = _REPO / "plugins" / "shared" / "pipeline" / "core" / "llm_core" / "plugin.json"


@pytest.fixture(scope="module")
def manifest() -> dict:
    return json.loads(_MANIFEST.read_text(encoding="utf-8"))


class TestStateReadsCoverage:
    """state.reads 是切片投喂白名单：声明非空后，未声明键对插件不可见。"""

    @pytest.mark.parametrize(
        ("key", "why"),
        [
            ("track.llm_usage", "跨轮累计基准——缺则累计退化为每轮覆盖"),
            ("track.messages_chars", "W2a 字符锚快照——缺则上下文粗门增量支恒 0"),
        ],
    )
    def test_key_declared(self, manifest: dict, key: str, why: str) -> None:
        reads = manifest.get("state", {}).get("reads", [])
        assert key in reads, f"state.reads 缺 {key}（{why}）——切片投喂下该键恒不可见"

    @pytest.mark.parametrize(
        "key", ["track.execution_stats", "run_started_at", "llm_usage"],
    )
    def test_retired_keys_not_declared(self, manifest: dict, key: str) -> None:
        """退役键不得留在任何声明面（死声明 = 漂移源）。"""
        reads = manifest.get("state", {}).get("reads", [])
        persistent = manifest.get("persistent_fields", [])
        exported = manifest.get("export_fields", [])
        assert key not in reads, f"state.reads 不应再声明已退役键 {key}"
        assert key not in persistent, f"persistent_fields 不应再声明已退役键 {key}"
        assert key not in exported, f"export_fields 不应再声明已退役键 {key}"

    def test_reads_still_whitelist_not_empty(self, manifest: dict) -> None:
        """reads 不得被清空：空 = 全量投喂，会让 llm_core 每步收整包 state。"""
        reads = manifest.get("state", {}).get("reads", [])
        assert reads, "reads 清空会退回全量投喂（每次调用整包 state 过境）"

    def test_messages_declared_for_request_assembly(self, manifest: dict) -> None:
        """请求装配的基础读面：消息与工具 schema 必须在声明内。"""
        reads = manifest.get("state", {}).get("reads", [])
        for key in ("messages", "system_message", "tool_schemas"):
            assert key in reads, f"请求装配读面缺 {key}"


class TestPersistenceAndExport:
    """persistent_fields 落库面 + export_fields 出口面（原 track 声明同款）。"""

    @pytest.mark.parametrize(
        "key",
        ["track.llm_usage", "track.total_tokens", "llm_model"],
    )
    def test_persistent_field_declared(self, manifest: dict, key: str) -> None:
        persistent = manifest.get("persistent_fields", [])
        assert key in persistent, f"persistent_fields 缺 {key}——累计值不落库（冷读全空）"

    @pytest.mark.parametrize(
        "key",
        ["track.llm_usage", "track.total_tokens"],
    )
    def test_export_field_declared(self, manifest: dict, key: str) -> None:
        exported = manifest.get("export_fields", [])
        assert key in exported, f"export_fields 缺 {key}——state 摘要面读不到（前端/通知断供）"


class TestCapabilityGrants:
    """G6 白名单制：granted_capabilities 声明非空即白名单，未列出的 namespace 一律拒绝。"""

    @pytest.mark.parametrize("cap", ["frontend", "metrics"])
    def test_observation_exit_granted(self, manifest: dict, cap: str) -> None:
        grants = manifest.get("granted_capabilities", [])
        assert cap in grants, (
            f"granted_capabilities 缺 {cap}——G6 单点授权拒绝（capability_router "
            f"check_grant 白名单制），该观察出口恒不可用"
        )

    def test_tool_executor_grant_preserved(self, manifest: dict) -> None:
        """原有授权不得在并入中丢失（LLM 调用通道）。"""
        grants = manifest.get("granted_capabilities", [])
        assert "tool-executor" in grants


class TestTrackStepRetired:
    """track 摘除后的装配面：管道步骤不再引用已删插件。"""

    def test_autonomous_pipeline_has_no_track_step(self) -> None:
        pipeline = (_REPO / "config" / "pipelines" / "autonomous.yaml").read_text(encoding="utf-8")
        assert "pipeline_track" not in pipeline, (
            "autonomous.yaml 仍引用 pipeline_track——插件目录已删，装载期会报未知插件"
        )

    def test_track_plugin_dir_removed(self) -> None:
        """并入完成态：原插件目录不存在（避免同 id 双装载与漂移）。"""
        assert not (_REPO / "plugins" / "shared" / "pipeline" / "output" / "track").exists()
