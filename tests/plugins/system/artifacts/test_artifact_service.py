"""ArtifactService / AnnotationService 单元测试——纯内存 CRUD + 版本链 + diff。

不触 DB / LLM / 网络：两个 service 在 __init__ 时即纯 dict 存储。
覆盖单例 get/reset、创建、查询、版本递增与回溯、unified diff、批注状态流转。
"""

from __future__ import annotations

from typing import Any

import pytest

pytestmark = pytest.mark.unit


# ============================================================
# 辅助
# ============================================================


@pytest.fixture
def svc() -> Any:
    """每个测试一个干净的 ArtifactService 实例。"""
    from artifacts.artifact_service import ArtifactService

    return ArtifactService()


@pytest.fixture
def ann_svc() -> Any:
    """每个测试一个干净的 AnnotationService 实例。"""
    from artifacts.annotation_service import AnnotationService

    return AnnotationService()


# ============================================================
# 单例
# ============================================================


class TestSingleton:
    def test_get_artifact_service_单例(self) -> None:
        from artifacts.artifact_service import (
            get_artifact_service,
            reset_artifact_service,
        )

        reset_artifact_service()
        a = get_artifact_service()
        b = get_artifact_service()
        assert a is b
        reset_artifact_service()
        c = get_artifact_service()
        assert c is not a

    def test_get_annotation_service_单例(self) -> None:
        from artifacts.annotation_service import (
            get_annotation_service,
            reset_annotation_service,
        )

        reset_annotation_service()
        a = get_annotation_service()
        b = get_annotation_service()
        assert a is b
        reset_annotation_service()


# ============================================================
# Artifact CRUD
# ============================================================


class TestArtifactCRUD:
    @pytest.mark.asyncio
    async def test_创建返回Artifact且字段落库(self, svc: Any) -> None:
        from artifacts.models import ArtifactType

        a = await svc.create_artifact(
            task_id="t1",
            title="hello",
            artifact_type="text",
            content="abc",
            file_path="/sandbox/f.txt",
            metadata={"k": "v"},
        )
        assert a.task_id == "t1"
        assert a.title == "hello"
        assert a.artifact_type == ArtifactType.TEXT
        assert a.content == "abc"
        assert a.file_path == "/sandbox/f.txt"
        assert a.metadata == {"k": "v"}
        assert a.version == 1
        assert a.parent_artifact_id is None
        assert a.id  # 非空
        # 能查回
        assert (await svc.get_artifact(a.id)).id == a.id

    @pytest.mark.asyncio
    async def test_用ArtifactType枚举创建也行(self, svc: Any) -> None:
        from artifacts.models import ArtifactType

        a = await svc.create_artifact(
            task_id="t1", title="img", artifact_type=ArtifactType.IMAGE
        )
        assert a.artifact_type == ArtifactType.IMAGE

    @pytest.mark.asyncio
    async def test_未给metadata默认空字典(self, svc: Any) -> None:
        a = await svc.create_artifact(task_id="t1", title="x", artifact_type="text")
        assert a.metadata == {}

    @pytest.mark.asyncio
    async def test_get_不存在返回None(self, svc: Any) -> None:
        assert await svc.get_artifact("nope") is None

    @pytest.mark.asyncio
    async def test_list_by_task分页与total(self, svc: Any) -> None:
        for i in range(5):
            await svc.create_artifact(
                task_id="t1", title=f"n{i}", artifact_type="text"
            )
        await svc.create_artifact(task_id="t2", title="other", artifact_type="text")

        page = await svc.list_artifacts_by_task("t1", limit=2, offset=1)
        assert page["total"] == 5
        assert len(page["items"]) == 2
        assert page["items"][0]["title"] == "n1"

    @pytest.mark.asyncio
    async def test_list_by_task空任务返回空(self, svc: Any) -> None:
        page = await svc.list_artifacts_by_task("nope")
        assert page == {"items": [], "total": 0}

    @pytest.mark.asyncio
    async def test_delete_存在返回True且查不到(self, svc: Any) -> None:
        a = await svc.create_artifact(task_id="t1", title="x", artifact_type="text")
        assert await svc.delete_artifact(a.id) is True
        assert await svc.get_artifact(a.id) is None
        # task 索引里也移除了
        page = await svc.list_artifacts_by_task("t1")
        assert page["total"] == 0

    @pytest.mark.asyncio
    async def test_delete_不存在返回False(self, svc: Any) -> None:
        assert await svc.delete_artifact("nope") is False


# ============================================================
# 版本管理
# ============================================================


class TestVersioning:
    @pytest.mark.asyncio
    async def test_update_创建新版本且版本号递增(self, svc: Any) -> None:
        a = await svc.create_artifact(
            task_id="t1", title="doc", artifact_type="text", content="v1"
        )
        b = await svc.update_artifact(a.id, content="v2")
        c = await svc.update_artifact(b.id, content="v3")

        assert b is not None and c is not None
        assert b.version == 2 and c.version == 3
        assert b.parent_artifact_id == a.id
        assert c.parent_artifact_id == b.id
        # 老版本仍在
        assert (await svc.get_artifact(a.id)).content == "v1"

    @pytest.mark.asyncio
    async def test_update_部分字段保留旧值(self, svc: Any) -> None:
        a = await svc.create_artifact(
            task_id="t1",
            title="doc",
            artifact_type="text",
            content="v1",
            metadata={"a": 1},
        )
        b = await svc.update_artifact(a.id, title="new-title")  # 只改 title

        assert b is not None
        assert b.title == "new-title"
        assert b.content == "v1"  # 保留
        assert b.metadata == {"a": 1}  # 保留

    @pytest.mark.asyncio
    async def test_update_metadata合并而非替换(self, svc: Any) -> None:
        a = await svc.create_artifact(
            task_id="t1",
            title="d",
            artifact_type="text",
            metadata={"a": 1, "b": 2},
        )
        b = await svc.update_artifact(a.id, metadata={"b": 99, "c": 3})
        assert b.metadata == {"a": 1, "b": 99, "c": 3}

    @pytest.mark.asyncio
    async def test_update_不存在返回None(self, svc: Any) -> None:
        assert await svc.update_artifact("nope", content="x") is None

    @pytest.mark.asyncio
    async def test_get_version_history_向上追溯链(self, svc: Any) -> None:
        a = await svc.create_artifact(
            task_id="t1", title="d", artifact_type="text", content="1"
        )
        b = await svc.update_artifact(a.id, content="2")
        c = await svc.update_artifact(b.id, content="3")

        hist = await svc.get_version_history(c.id)
        assert hist["total"] == 3
        # 降序
        versions = [v["version"] for v in hist["items"]]
        assert versions == [3, 2, 1]

    @pytest.mark.asyncio
    async def test_get_version_history_不存在返回空(self, svc: Any) -> None:
        hist = await svc.get_version_history("nope")
        assert hist == {"items": [], "total": 0}

    @pytest.mark.asyncio
    async def test_get_version_diff_返回unified_diff(self, svc: Any) -> None:
        a = await svc.create_artifact(
            task_id="t1", title="d", artifact_type="text", content="line1\nline2"
        )
        b = await svc.update_artifact(a.id, content="line1\nline2-changed")

        # 注意：版本历史是从最新版本向上回溯 parent 链，故要从最新版本 b.id 入手
        diff = await svc.get_version_diff(b.id, from_version=1, to_version=2)
        assert diff["from_version"] == 1
        assert diff["to_version"] == 2
        # unified_diff 含 @@ 头与 -/+ 行
        assert "@@" in diff["diff"]
        assert "-line2" in diff["diff"]
        assert "+line2-changed" in diff["diff"]

    @pytest.mark.asyncio
    async def test_get_version_diff_版本缺失用空串(self, svc: Any) -> None:
        a = await svc.create_artifact(
            task_id="t1", title="d", artifact_type="text", content="x"
        )
        # from_version=99 不存在 → content="" → 全部作为新增
        diff = await svc.get_version_diff(a.id, from_version=99, to_version=1)
        assert "+x" in diff["diff"]


# ============================================================
# Annotation CRUD + 状态
# ============================================================


class TestAnnotationService:
    @pytest.mark.asyncio
    async def test_创建批注并取回(self, ann_svc: Any) -> None:
        from artifacts.models import AnnotationTarget

        ann = await ann_svc.create_annotation(
            artifact_id="art-1",
            target_type="text_selection",
            target_data={"start": 0, "end": 5},
            content="note",
            author_type="agent",
            author_id="bot",
        )
        assert ann.artifact_id == "art-1"
        assert ann.target_type == AnnotationTarget.TEXT_SELECTION
        assert ann.content == "note"
        assert ann.author_type == "agent"
        assert (await ann_svc.get_annotation(ann.id)).id == ann.id

    @pytest.mark.asyncio
    async def test_list_by_artifact_按状态过滤(self, ann_svc: Any) -> None:
        a1 = await ann_svc.create_annotation(
            "art-1", "whole_artifact", {}, "n1"
        )
        await ann_svc.create_annotation("art-1", "whole_artifact", {}, "n2")
        await ann_svc.resolve_annotation(a1.id)

        active = await ann_svc.list_annotations_by_artifact("art-1", status="active")
        resolved = await ann_svc.list_annotations_by_artifact(
            "art-1", status="resolved"
        )
        assert active["total"] == 1 and active["items"][0]["content"] == "n2"
        assert resolved["total"] == 1 and resolved["items"][0]["content"] == "n1"

    @pytest.mark.asyncio
    async def test_update_批注内容与target_data(self, ann_svc: Any) -> None:
        ann = await ann_svc.create_annotation(
            "art-1", "whole_artifact", {"x": 1}, "old"
        )
        upd = await ann_svc.update_annotation(
            ann.id, content="new", target_data={"x": 2}
        )
        assert upd is not None
        assert upd.content == "new"
        assert upd.target_data == {"x": 2}

    @pytest.mark.asyncio
    async def test_update_不存在返回None(self, ann_svc: Any) -> None:
        assert await ann_svc.update_annotation("nope", content="x") is None

    @pytest.mark.asyncio
    async def test_delete_存在返回True索引也清(self, ann_svc: Any) -> None:
        ann = await ann_svc.create_annotation(
            "art-1", "whole_artifact", {}, "n"
        )
        assert await ann_svc.delete_annotation(ann.id) is True
        assert await ann_svc.get_annotation(ann.id) is None
        page = await ann_svc.list_annotations_by_artifact("art-1")
        assert page["total"] == 0

    @pytest.mark.asyncio
    async def test_delete_不存在返回False(self, ann_svc: Any) -> None:
        assert await ann_svc.delete_annotation("nope") is False

    @pytest.mark.asyncio
    async def test_resolve_置resolved状态与时间戳(self, ann_svc: Any) -> None:
        from artifacts.models import AnnotationStatus

        ann = await ann_svc.create_annotation(
            "art-1", "whole_artifact", {}, "n"
        )
        assert ann.status == AnnotationStatus.ACTIVE
        resolved = await ann_svc.resolve_annotation(ann.id)
        assert resolved is not None
        assert resolved.status == AnnotationStatus.RESOLVED
        assert resolved.resolved_at is not None

    @pytest.mark.asyncio
    async def test_resolve_不存在返回None(self, ann_svc: Any) -> None:
        assert await ann_svc.resolve_annotation("nope") is None

    @pytest.mark.asyncio
    async def test_list_受limit限制(self, ann_svc: Any) -> None:
        for i in range(5):
            await ann_svc.create_annotation(
                "art-1", "whole_artifact", {}, f"n{i}"
            )
        page = await ann_svc.list_annotations_by_artifact("art-1", limit=2)
        assert page["total"] == 2  # total 是 items 长度（截断后）


# ============================================================
# 模型 round-trip
# ============================================================


class TestModelsRoundTrip:
    def test_Artifact_to_dict_from_dict(self) -> None:
        from artifacts.models import Artifact, ArtifactType

        a = Artifact(
            id="a1",
            task_id="t1",
            title="x",
            artifact_type=ArtifactType.CODE,
            content="print(1)",
            version=3,
            metadata={"lang": "py"},
        )
        d = a.to_dict()
        assert d["artifact_type"] == "code"
        assert d["version"] == 3
        a2 = Artifact.from_dict(d)
        assert a2.artifact_type == ArtifactType.CODE
        assert a2.content == "print(1)"

    def test_Annotation_to_dict_无resolved_at不输出该键(self) -> None:
        from artifacts.models import Annotation

        ann = Annotation(content="n")
        d = ann.to_dict()
        assert "resolved_at" not in d

    def test_Annotation_to_dict_有resolved_at输出(self) -> None:
        from artifacts.models import Annotation

        ann = Annotation(content="n", resolved_at="2026-01-01T00:00:00Z")
        d = ann.to_dict()
        assert d["resolved_at"] == "2026-01-01T00:00:00Z"
