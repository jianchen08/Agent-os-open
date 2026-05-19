"""TagService Tag 服务测试。

测试 TagService 的构造函数、get_or_create、link_to_memory、
get_tag / list_tags 以及磁盘持久化。
"""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from memory.tag_service import TagService


# ============================================================
# Fixtures
# ============================================================


@pytest.fixture
def temp_dir() -> str:
    """创建临时目录用于测试。"""
    d = tempfile.mkdtemp(prefix="test_tag_")
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def tag_svc(temp_dir: str) -> TagService:
    """创建无外部依赖的 TagService。"""
    return TagService(data_dir=temp_dir)


@pytest.fixture
def embedding_fn() -> AsyncMock:
    """创建 mock embedding 函数。"""
    fn = AsyncMock(return_value=[0.1, 0.2, 0.3])
    return fn


@pytest.fixture
def tag_svc_with_embedding(temp_dir: str, embedding_fn: AsyncMock) -> TagService:
    """创建带 embedding 函数的 TagService。"""
    return TagService(embedding_fn=embedding_fn, data_dir=temp_dir)


@pytest.fixture
def vector_retriever() -> AsyncMock:
    """创建 mock vector_retriever。"""
    vr = AsyncMock()
    vr.save_tag = AsyncMock(return_value=1)
    vr.update_cooccurrence = AsyncMock()
    return vr


@pytest.fixture
def tag_svc_with_vr(
    temp_dir: str, embedding_fn: AsyncMock, vector_retriever: AsyncMock,
) -> TagService:
    """创建带 vector_retriever 的 TagService。"""
    return TagService(
        embedding_fn=embedding_fn,
        vector_retriever=vector_retriever,
        data_dir=temp_dir,
    )


# ============================================================
# 1. 构造函数测试
# ============================================================


class TestTagServiceInit:
    """测试 TagService 初始化。"""

    def test_创建tags目录(self, temp_dir: str) -> None:
        """初始化时应创建 tags 子目录。"""
        TagService(data_dir=temp_dir)
        tags_dir = Path(temp_dir) / "tags"
        assert tags_dir.exists()
        assert tags_dir.is_dir()

    def test_加载空目录(self, temp_dir: str) -> None:
        """初始化空目录时缓存应为空。"""
        svc = TagService(data_dir=temp_dir)
        assert len(svc._cache) == 0

    def test_加载已有tag文件(self, temp_dir: str) -> None:
        """初始化时应从磁盘加载已有 tag 文件。"""
        tags_dir = Path(temp_dir) / "tags"
        tags_dir.mkdir(parents=True, exist_ok=True)
        tag_data = {"id": 1, "name": "python", "frequency": 5}
        (tags_dir / "python.json").write_text(
            json.dumps(tag_data, ensure_ascii=False), encoding="utf-8",
        )
        svc = TagService(data_dir=temp_dir)
        assert "python" in svc._cache
        assert svc._cache["python"].frequency == 5

    def test_加载损坏文件时跳过(self, temp_dir: str) -> None:
        """加载损坏的 JSON 文件时应跳过。"""
        tags_dir = Path(temp_dir) / "tags"
        tags_dir.mkdir(parents=True, exist_ok=True)
        (tags_dir / "bad.json").write_text("invalid json{{{", encoding="utf-8")
        svc = TagService(data_dir=temp_dir)
        assert len(svc._cache) == 0


# ============================================================
# 2. get_or_create 测试
# ============================================================


class TestGetOrCreate:
    """测试 get_or_create 方法。"""

    @pytest.mark.asyncio
    async def test_新建tag(self, tag_svc: TagService) -> None:
        """首次查询应创建新 tag。"""
        tag = await tag_svc.get_or_create("python")
        assert tag.name == "python"
        assert tag.frequency == 1
        assert "python" in tag_svc._cache

    @pytest.mark.asyncio
    async def test_新建tag_带embedding(self, tag_svc_with_embedding: TagService) -> None:
        """新建 tag 时应调用 embedding_fn 生成向量。"""
        tag = await tag_svc_with_embedding.get_or_create("python")
        assert tag.vector == [0.1, 0.2, 0.3]

    @pytest.mark.asyncio
    async def test_新建tag_无embedding时向量为空(self, tag_svc: TagService) -> None:
        """无 embedding_fn 时向量应为空列表。"""
        tag = await tag_svc.get_or_create("python")
        assert tag.vector is None or tag.vector == []

    @pytest.mark.asyncio
    async def test_新建tag_写入PG(self, tag_svc_with_vr: TagService) -> None:
        """有 vector_retriever 时应调用 save_tag。"""
        tag = await tag_svc_with_vr.get_or_create("python")
        tag_svc_with_vr._vector_retriever.save_tag.assert_called_once()
        assert tag.id == 1

    @pytest.mark.asyncio
    async def test_缓存命中(self, tag_svc: TagService) -> None:
        """第二次查询应命中缓存。"""
        tag1 = await tag_svc.get_or_create("python")
        tag2 = await tag_svc.get_or_create("python")
        assert tag1 is tag2
        assert tag2.frequency == 2

    @pytest.mark.asyncio
    async def test_频率递增(self, tag_svc: TagService) -> None:
        """多次查询同一 tag 应递增 frequency。"""
        await tag_svc.get_or_create("python")
        await tag_svc.get_or_create("python")
        tag = await tag_svc.get_or_create("python")
        assert tag.frequency == 3

    @pytest.mark.asyncio
    async def test_缓存命中时更新PG(self, tag_svc_with_vr: TagService) -> None:
        """缓存命中时应更新 PG。"""
        await tag_svc_with_vr.get_or_create("python")
        await tag_svc_with_vr.get_or_create("python")
        # save_tag 应被调用两次（创建 + 更新）
        assert tag_svc_with_vr._vector_retriever.save_tag.call_count == 2

    @pytest.mark.asyncio
    async def test_embedding失败时不抛异常(self, temp_dir: str) -> None:
        """embedding_fn 失败时应创建无向量的 tag。"""
        bad_fn = AsyncMock(side_effect=Exception("embedding 错误"))
        svc = TagService(embedding_fn=bad_fn, data_dir=temp_dir)
        tag = await svc.get_or_create("test")
        assert tag.name == "test"
        assert tag.vector == []

    @pytest.mark.asyncio
    async def test_PG写入失败时不抛异常(self, temp_dir: str) -> None:
        """PG 写入失败时应创建 tag 但 id 为 0。"""
        vr = AsyncMock()
        vr.save_tag = AsyncMock(side_effect=Exception("PG 错误"))
        svc = TagService(vector_retriever=vr, data_dir=temp_dir)
        tag = await svc.get_or_create("test")
        assert tag.name == "test"
        assert tag.id == 0


# ============================================================
# 3. link_to_memory 测试
# ============================================================


class TestLinkToMemory:
    """测试 link_to_memory 方法。"""

    @pytest.mark.asyncio
    async def test_关联单个关键词(self, tag_svc: TagService) -> None:
        """关联单个关键词应创建 tag 但不触发共现更新。"""
        await tag_svc.link_to_memory("mem-1", "episode", ["python"])
        tag = await tag_svc.get_tag("python")
        assert tag is not None
        assert tag.frequency >= 1

    @pytest.mark.asyncio
    async def test_关联多个关键词触发共现(self, tag_svc_with_vr: TagService) -> None:
        """关联 2 个以上关键词应触发共现更新。"""
        await tag_svc_with_vr.link_to_memory("mem-1", "episode", ["python", "flask"])
        tag_svc_with_vr._vector_retriever.update_cooccurrence.assert_called()

    @pytest.mark.asyncio
    async def test_单个关键词不触发共现(self, tag_svc_with_vr: TagService) -> None:
        """只关联 1 个关键词不应触发共现更新。"""
        await tag_svc_with_vr.link_to_memory("mem-1", "episode", ["python"])
        tag_svc_with_vr._vector_retriever.update_cooccurrence.assert_not_called()

    @pytest.mark.asyncio
    async def test_空关键词列表(self, tag_svc: TagService) -> None:
        """空关键词列表不应创建任何 tag。"""
        await tag_svc.link_to_memory("mem-1", "episode", [])
        assert len(tag_svc._cache) == 0


# ============================================================
# 4. get_tag / list_tags 测试
# ============================================================


class TestTagQueries:
    """测试 get_tag 和 list_tags。"""

    @pytest.mark.asyncio
    async def test_get_tag_存在(self, tag_svc: TagService) -> None:
        """获取存在的 tag 应返回 TagInfo。"""
        await tag_svc.get_or_create("python")
        tag = await tag_svc.get_tag("python")
        assert tag is not None
        assert tag.name == "python"

    @pytest.mark.asyncio
    async def test_get_tag_不存在(self, tag_svc: TagService) -> None:
        """获取不存在的 tag 应返回 None。"""
        tag = await tag_svc.get_tag("nonexistent")
        assert tag is None

    @pytest.mark.asyncio
    async def test_list_tags_按频率降序(self, tag_svc: TagService) -> None:
        """list_tags 应按频率降序排列。"""
        await tag_svc.get_or_create("python")  # freq=1
        await tag_svc.get_or_create("flask")
        await tag_svc.get_or_create("python")  # freq=2
        await tag_svc.get_or_create("django")
        await tag_svc.get_or_create("python")  # freq=3
        tags = await tag_svc.list_tags()
        assert len(tags) == 3
        assert tags[0].name == "python"
        assert tags[0].frequency == 3

    @pytest.mark.asyncio
    async def test_list_tags_limit(self, tag_svc: TagService) -> None:
        """list_tags 应支持 limit 参数。"""
        for i in range(10):
            await tag_svc.get_or_create(f"tag_{i}")
        tags = await tag_svc.list_tags(limit=5)
        assert len(tags) == 5

    @pytest.mark.asyncio
    async def test_list_tags_空缓存(self, tag_svc: TagService) -> None:
        """空缓存时 list_tags 应返回空列表。"""
        tags = await tag_svc.list_tags()
        assert tags == []


# ============================================================
# 5. _save_to_disk / _load_from_disk 测试
# ============================================================


class TestPersistence:
    """测试磁盘持久化。"""

    @pytest.mark.asyncio
    async def test_保存到磁盘(self, temp_dir: str) -> None:
        """get_or_create 后应生成 JSON 文件。"""
        svc = TagService(data_dir=temp_dir)
        await svc.get_or_create("python")
        tags_dir = Path(temp_dir) / "tags"
        json_files = list(tags_dir.glob("*.json"))
        assert len(json_files) == 1
        data = json.loads(json_files[0].read_text(encoding="utf-8"))
        assert data["name"] == "python"
        assert data["frequency"] == 1

    @pytest.mark.asyncio
    async def test_从磁盘重新加载(self, temp_dir: str) -> None:
        """保存后重新创建 TagService 应能加载已有 tag。"""
        svc1 = TagService(data_dir=temp_dir)
        await svc1.get_or_create("python")
        await svc1.get_or_create("python")  # freq=2

        svc2 = TagService(data_dir=temp_dir)
        tag = await svc2.get_tag("python")
        assert tag is not None
        assert tag.frequency == 2

    @pytest.mark.asyncio
    async def test_特殊字符名称安全保存(self, temp_dir: str) -> None:
        """含特殊字符的 tag 名称应安全保存（路径替换）。"""
        svc = TagService(data_dir=temp_dir)
        await svc.get_or_create("a/b:c\\d")
        tags_dir = Path(temp_dir) / "tags"
        json_files = list(tags_dir.glob("*.json"))
        assert len(json_files) == 1
        data = json.loads(json_files[0].read_text(encoding="utf-8"))
        assert data["name"] == "a/b:c\\d"

    @pytest.mark.asyncio
    async def test_频率更新后持久化(self, temp_dir: str) -> None:
        """频率更新后应更新磁盘文件。"""
        svc = TagService(data_dir=temp_dir)
        await svc.get_or_create("python")  # freq=1
        await svc.get_or_create("python")  # freq=2
        tags_dir = Path(temp_dir) / "tags"
        data = json.loads(
            (tags_dir / "python.json").read_text(encoding="utf-8"),
        )
        assert data["frequency"] == 2
