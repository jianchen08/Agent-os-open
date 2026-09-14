# @feature: FP-0.2.二 内部模块 manifest | @ci: python-coverage
"""artifacts 缺口分支补测（HEAD coverage.xml 缺行，2026-09-14）。

覆盖目标（行号语义经源码逐行确认）：
- models.py:175-181（Annotation.from_dict 全字段反序列化：target_type /
  status 字符串归一 + 可选项透传）
- artifact_service.py:192（get_version_history 的父链断点：parent_id 指向
  已不存在的制品时停止追溯，不产悬空版本）

模型与服务的父链走真实内存实例（无 mock）；断点场景通过删除父制品构造。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SYSTEM_DIR = _REPO_ROOT / "plugins" / "shared" / "system"
# 插件内部用 `from artifacts.models import ...`（包名 artifacts），
# 故把 system/ 放到 sys.path 使包可解析（与 tests/plugins/system/artifacts/conftest.py 同源）。
if str(_SYSTEM_DIR) not in sys.path:
    sys.path.insert(0, str(_SYSTEM_DIR))

from artifacts.artifact_service import ArtifactService  # noqa: E402
from artifacts.models import (  # noqa: E402
    Annotation,
    AnnotationStatus,
    AnnotationTarget,
)


def _run(coro: Any) -> Any:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ═══════════════════════════════════════════════════════════
# Annotation.from_dict：全字段反序列化与枚举归一
# ═══════════════════════════════════════════════════════════


class TestAnnotationFromDict:
    @pytest.mark.parametrize(
        "raw_target",
        ["whole_artifact", "text_selection", "image_region", "video_timeline"],
    )
    def test_target_type_strings_normalized(self, raw_target: str) -> None:
        """target_type 字符串 → 枚举（176-177）。"""
        annotation = Annotation.from_dict({"target_type": raw_target})
        assert annotation.target_type is AnnotationTarget(raw_target)

    @pytest.mark.parametrize(
        "raw_status",
        ["active", "resolved", "dismissed"],
    )
    def test_status_strings_normalized(self, raw_status: str) -> None:
        """status 字符串 → 枚举（179-180）。"""
        annotation = Annotation.from_dict({"status": raw_status})
        assert annotation.status is AnnotationStatus(raw_status)

    def test_full_payload_roundtrip(self) -> None:
        """全字段：往返后所有可观察字段保持等价（181-191）。"""
        original = Annotation(
            id="an-1",
            artifact_id="art-9",
            target_type=AnnotationTarget.IMAGE_REGION,
            target_data={"x": 1, "y": 2, "w": 10, "h": 20},
            content="这里需要调整",
            author_type="agent",
            author_id="agent-7",
            status=AnnotationStatus.RESOLVED,
            created_at="2026-03-03T00:00:00+00:00",
            resolved_at="2026-03-03T01:00:00+00:00",
        )
        restored = Annotation.from_dict(original.to_dict())
        assert restored.id == original.id
        assert restored.artifact_id == original.artifact_id
        assert restored.target_type is original.target_type
        assert restored.target_data == original.target_data
        assert restored.content == original.content
        assert restored.author_type == "agent"
        assert restored.author_id == "agent-7"
        assert restored.status is original.status
        assert restored.resolved_at == original.resolved_at

    def test_enum_instances_pass_through(self) -> None:
        """已是枚举实例时不重复构造（isinstance 判假分支）。"""
        annotation = Annotation.from_dict({
            "target_type": AnnotationTarget.VIDEO_TIMELINE,
            "status": AnnotationStatus.DISMISSED,
        })
        assert annotation.target_type is AnnotationTarget.VIDEO_TIMELINE
        assert annotation.status is AnnotationStatus.DISMISSED

    def test_empty_payload_defaults(self) -> None:
        """空字典：id 自动生成、target_type/status 走默认、resolved_at 缺省。"""
        annotation = Annotation.from_dict({})
        assert annotation.id and len(annotation.id) == 12
        assert annotation.target_type is AnnotationTarget.WHOLE_ARTIFACT
        assert annotation.status is AnnotationStatus.ACTIVE
        assert annotation.target_data == {}
        assert annotation.resolved_at is None
        assert "resolved_at" not in annotation.to_dict()

    def test_unknown_enum_value_raises(self) -> None:
        """非法枚举值 fail-closed（不静默落默认）。"""
        with pytest.raises(ValueError):
            Annotation.from_dict({"target_type": "telepathy"})
        with pytest.raises(ValueError):
            Annotation.from_dict({"status": "maybe"})


# ═══════════════════════════════════════════════════════════
# artifact_service.get_version_history：父链断点
# ═══════════════════════════════════════════════════════════


class TestVersionHistoryBrokenChain:
    """父链指向已删除制品 → 停止追溯（192），不产悬空版本条目。"""

    async def test_missing_parent_stops_traversal(self) -> None:
        """父链中段断裂 → 只收录断点以下部分，更早的祖先不再可达（192）。"""
        service = ArtifactService()
        v1 = await service.create_artifact(
            task_id="t-hist", title="v1", artifact_type="text", content="one"
        )
        v2 = await service.update_artifact(v1.id, content="two")
        v3 = await service.update_artifact(v2.id, content="three")
        v4 = await service.update_artifact(v3.id, content="four")

        # 删除 v2 → v1 成为不可达祖先（v3.parent=v2 断了）
        service._artifacts.pop(v2.id)

        history = await service.get_version_history(v4.id)

        assert history["total"] == 2, "断点以上的祖先不应被收录"
        versions = [item["version"] for item in history["items"]]
        assert versions == [4, 3]
        assert versions == sorted(versions, reverse=True), "按版本号降序"
        ids = {item["id"] for item in history["items"]}
        assert ids == {v4.id, v3.id}
        assert v1.id not in ids and v2.id not in ids

    async def test_intact_chain_returns_all_ancestors(self) -> None:
        """对照组：父链完整 → 全链版本按降序返回（证明断点截断非恒 2 条）。"""
        service = ArtifactService()
        root = await service.create_artifact(
            task_id="t-ok", title="v1", artifact_type="text", content="one"
        )
        mid = await service.update_artifact(root.id, content="two")
        leaf = await service.update_artifact(mid.id, content="three")

        history = await service.get_version_history(leaf.id)

        assert history["total"] == 3
        versions = [item["version"] for item in history["items"]]
        assert versions == [3, 2, 1]
        assert versions == sorted(versions, reverse=True)

    async def test_cycle_in_parent_chain_terminates(self) -> None:
        """环状父链（A→B→A）→ 不无限循环，结果集去重（seen 守卫）。"""
        service = ArtifactService()
        first = await service.create_artifact(
            task_id="t-cycle", title="c1", artifact_type="text", content="1"
        )
        second = await service.update_artifact(first.id, content="2")
        service._artifacts[first.id].parent_artifact_id = second.id  # 构造环

        history = await service.get_version_history(second.id)

        ids = [item["id"] for item in history["items"]]
        assert len(ids) == len(set(ids)), "环状链不得重复收录"
        assert set(ids) == {first.id, second.id}

    async def test_unknown_artifact_returns_empty(self) -> None:
        """根节点不存在 → 空结果（对照：断点与不存在是两种空）。"""
        service = ArtifactService()
        assert await service.get_version_history("never-created") == {"items": [], "total": 0}
