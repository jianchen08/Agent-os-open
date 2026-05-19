"""ChunkService 压缩块服务测试。

测试 ChunkService 的构造函数、save / load / delete、
find_by_session / find_by_user 以及磁盘持久化。
"""

from __future__ import annotations

import json
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from memory.chunk_service import ChunkService
from memory.types import ChunkData


# ============================================================
# Fixtures
# ============================================================


@pytest.fixture
def temp_dir() -> str:
    """创建临时目录用于测试。"""
    d = tempfile.mkdtemp(prefix="test_chunk_")
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def chunk_svc(temp_dir: str) -> ChunkService:
    """创建无外部依赖的 ChunkService。"""
    return ChunkService(data_dir=temp_dir)


def _make_chunk(**overrides) -> ChunkData:
    """创建测试用 ChunkData。"""
    # ChunkData 实际字段
    chunk_fields = {
        "id", "pipeline_run_id", "session_id", "layer", "content",
        "l2_content", "token_count", "message_count", "sequence_start",
        "sequence_end", "keywords", "graduated", "episode_id",
        "created_at",
    }
    # 分离 ChunkData 字段和动态属性
    chunk_kwargs = {k: v for k, v in overrides.items() if k in chunk_fields}
    extra_attrs = {k: v for k, v in overrides.items() if k not in chunk_fields}

    defaults = dict(
        id="chunk-1",
        pipeline_run_id="run-1",
        layer="L1",
        content="这是压缩块内容",
        token_count=100,
        message_count=5,
        keywords=["python", "flask"],
    )
    defaults.update(chunk_kwargs)
    chunk = ChunkData(**defaults)

    # 设置动态属性（user_id, session_id 等）
    for key, value in extra_attrs.items():
        setattr(chunk, key, value)

    return chunk


# ============================================================
# 1. 构造函数测试
# ============================================================


class TestChunkServiceInit:
    """测试 ChunkService 初始化。"""

    def test_创建chunks目录(self, temp_dir: str) -> None:
        """初始化时应创建 chunks 子目录。"""
        ChunkService(data_dir=temp_dir)
        chunks_dir = Path(temp_dir) / "chunks"
        assert chunks_dir.exists()
        assert chunks_dir.is_dir()

    def test_加载空目录(self, temp_dir: str) -> None:
        """初始化空目录时缓存应为空。"""
        svc = ChunkService(data_dir=temp_dir)
        assert len(svc._cache) == 0

    def test_加载已有文件(self, temp_dir: str) -> None:
        """初始化时应从磁盘加载已有压缩块文件。"""
        chunks_dir = Path(temp_dir) / "chunks"
        chunks_dir.mkdir(parents=True, exist_ok=True)
        chunk_data = _make_chunk()
        (chunks_dir / "chunk-1.json").write_text(
            json.dumps(chunk_data.to_dict(), ensure_ascii=False), encoding="utf-8",
        )
        svc = ChunkService(data_dir=temp_dir)
        assert "chunk-1" in svc._cache

    def test_加载损坏文件时跳过(self, temp_dir: str) -> None:
        """加载损坏的 JSON 文件时应跳过。"""
        chunks_dir = Path(temp_dir) / "chunks"
        chunks_dir.mkdir(parents=True, exist_ok=True)
        (chunks_dir / "bad.json").write_text("invalid json{{{", encoding="utf-8")
        svc = ChunkService(data_dir=temp_dir)
        assert len(svc._cache) == 0


# ============================================================
# 2. save 测试
# ============================================================


class TestChunkServiceSave:
    """测试 save 方法。"""

    @pytest.mark.asyncio
    async def test_基本保存(self, chunk_svc: ChunkService) -> None:
        """保存压缩块应返回 chunk ID。"""
        chunk = _make_chunk()
        cid = await chunk_svc.save(chunk)
        assert cid == "chunk-1"

    @pytest.mark.asyncio
    async def test_保存后加入缓存(self, chunk_svc: ChunkService) -> None:
        """保存后应加入内存缓存。"""
        chunk = _make_chunk()
        await chunk_svc.save(chunk)
        assert "chunk-1" in chunk_svc._cache

    @pytest.mark.asyncio
    async def test_保存后写磁盘(self, chunk_svc: ChunkService, temp_dir: str) -> None:
        """保存后应写入磁盘 JSON 文件。"""
        chunk = _make_chunk()
        await chunk_svc.save(chunk)
        chunks_dir = Path(temp_dir) / "chunks"
        assert (chunks_dir / "chunk-1.json").exists()

    @pytest.mark.asyncio
    async def test_有向量检索器时写入PG(self, temp_dir: str) -> None:
        """有 vector_retriever 时应尝试写入向量索引。"""
        vr = AsyncMock()
        vr.save_chunk_index = AsyncMock()
        vr._embedding_fn = AsyncMock(return_value=[0.1, 0.2])
        svc = ChunkService(vector_retriever=vr, data_dir=temp_dir)
        chunk = _make_chunk(content="测试内容", user_id="u1", session_id="s1")
        await svc.save(chunk)
        vr.save_chunk_index.assert_called_once()

    @pytest.mark.asyncio
    async def test_有tag服务时关联tag(self, temp_dir: str) -> None:
        """有 tag_service 且有关键词时应关联 tag。"""
        tag_svc = AsyncMock()
        tag_svc.link_to_memory = AsyncMock()
        svc = ChunkService(tag_service=tag_svc, data_dir=temp_dir)
        chunk = _make_chunk(keywords=["python", "flask"])
        await svc.save(chunk)
        tag_svc.link_to_memory.assert_called_once_with(
            memory_id="chunk-1",
            memory_type="chunk",
            keywords=["python", "flask"],
        )

    @pytest.mark.asyncio
    async def test_无关键词时不关联tag(self, temp_dir: str) -> None:
        """无关键词时不应调用 tag 服务。"""
        tag_svc = AsyncMock()
        tag_svc.link_to_memory = AsyncMock()
        svc = ChunkService(tag_service=tag_svc, data_dir=temp_dir)
        chunk = _make_chunk(keywords=[])
        await svc.save(chunk)
        tag_svc.link_to_memory.assert_not_called()

    @pytest.mark.asyncio
    async def test_覆盖保存(self, chunk_svc: ChunkService) -> None:
        """重复保存同一 ID 应覆盖。"""
        chunk1 = _make_chunk(content="内容1")
        chunk2 = _make_chunk(content="内容2")
        await chunk_svc.save(chunk1)
        await chunk_svc.save(chunk2)
        loaded = await chunk_svc.load("chunk-1")
        assert loaded.content == "内容2"


# ============================================================
# 3. load 测试
# ============================================================


class TestChunkServiceLoad:
    """测试 load 方法。"""

    @pytest.mark.asyncio
    async def test_加载存在的chunk(self, chunk_svc: ChunkService) -> None:
        """加载已保存的压缩块。"""
        chunk = _make_chunk()
        await chunk_svc.save(chunk)
        loaded = await chunk_svc.load("chunk-1")
        assert loaded is not None
        assert loaded.id == "chunk-1"
        assert loaded.content == "这是压缩块内容"

    @pytest.mark.asyncio
    async def test_加载不存在的chunk(self, chunk_svc: ChunkService) -> None:
        """加载不存在的压缩块应返回 None。"""
        result = await chunk_svc.load("nonexistent")
        assert result is None


# ============================================================
# 4. delete 测试
# ============================================================


class TestChunkServiceDelete:
    """测试 delete 方法。"""

    @pytest.mark.asyncio
    async def test_删除存在的chunk(self, chunk_svc: ChunkService) -> None:
        """删除已保存的压缩块。"""
        chunk = _make_chunk()
        await chunk_svc.save(chunk)
        success = await chunk_svc.delete("chunk-1")
        assert success is True
        assert await chunk_svc.load("chunk-1") is None

    @pytest.mark.asyncio
    async def test_删除后移除磁盘文件(self, chunk_svc: ChunkService, temp_dir: str) -> None:
        """删除后应移除磁盘 JSON 文件。"""
        chunk = _make_chunk()
        await chunk_svc.save(chunk)
        chunks_dir = Path(temp_dir) / "chunks"
        assert (chunks_dir / "chunk-1.json").exists()
        await chunk_svc.delete("chunk-1")
        assert not (chunks_dir / "chunk-1.json").exists()

    @pytest.mark.asyncio
    async def test_删除不存在的chunk(self, chunk_svc: ChunkService) -> None:
        """删除不存在的压缩块应返回 False。"""
        success = await chunk_svc.delete("nonexistent")
        assert success is False

    @pytest.mark.asyncio
    async def test_有向量检索器时删除PG索引(self, temp_dir: str) -> None:
        """有 vector_retriever 时应删除向量索引。"""
        vr = AsyncMock()
        vr.delete_chunk_index = AsyncMock()
        svc = ChunkService(vector_retriever=vr, data_dir=temp_dir)
        chunk = _make_chunk()
        await svc.save(chunk)
        await svc.delete("chunk-1")
        vr.delete_chunk_index.assert_called_once_with("chunk-1")


# ============================================================
# 5. find_by_pipeline 测试
# ============================================================


class TestFindByPipeline:
    """测试按管道运行 ID 查找。"""

    @pytest.mark.asyncio
    async def test_按pipeline_run_id查找(self, chunk_svc: ChunkService) -> None:
        """应返回指定管道的压缩块。"""
        await chunk_svc.save(_make_chunk(id="c1", pipeline_run_id="p1", layer="L1"))
        await chunk_svc.save(_make_chunk(id="c2", pipeline_run_id="p1", layer="L2"))
        await chunk_svc.save(_make_chunk(id="c3", pipeline_run_id="p2", layer="L1"))
        results = await chunk_svc.find_by_pipeline("p1")
        assert len(results) == 2

    @pytest.mark.asyncio
    async def test_按pipeline_run_id和layer过滤(self, chunk_svc: ChunkService) -> None:
        """应支持 layer 过滤。"""
        await chunk_svc.save(_make_chunk(id="c1", pipeline_run_id="p1", layer="L1"))
        await chunk_svc.save(_make_chunk(id="c2", pipeline_run_id="p1", layer="L2"))
        results = await chunk_svc.find_by_pipeline("p1", layer="L1")
        assert len(results) == 1
        assert results[0].layer == "L1"

    @pytest.mark.asyncio
    async def test_无匹配pipeline返回空(self, chunk_svc: ChunkService) -> None:
        """无匹配管道应返回空列表。"""
        await chunk_svc.save(_make_chunk(pipeline_run_id="p1"))
        results = await chunk_svc.find_by_pipeline("nonexistent")
        assert results == []

    @pytest.mark.asyncio
    async def test_按created_at排序(self, chunk_svc: ChunkService) -> None:
        """结果应按 created_at 升序排列。"""
        await chunk_svc.save(_make_chunk(id="c2", pipeline_run_id="p1", created_at=datetime(2024, 2, 1, tzinfo=UTC)))
        await chunk_svc.save(_make_chunk(id="c1", pipeline_run_id="p1", created_at=datetime(2024, 1, 1, tzinfo=UTC)))
        results = await chunk_svc.find_by_pipeline("p1")
        assert results[0].id == "c1"
        assert results[1].id == "c2"


# ============================================================
# 6. find_by_user 测试
# ============================================================


class TestFindByUser:
    """测试按用户查找。"""

    @pytest.mark.asyncio
    async def test_按user_id查找(self, chunk_svc: ChunkService) -> None:
        """应返回指定用户的压缩块。"""
        await chunk_svc.save(_make_chunk(id="c1", user_id="user-1"))
        await chunk_svc.save(_make_chunk(id="c2", user_id="user-1"))
        await chunk_svc.save(_make_chunk(id="c3", user_id="user-2"))
        results = await chunk_svc.find_by_user("user-1")
        assert len(results) == 2

    @pytest.mark.asyncio
    async def test_limit参数(self, chunk_svc: ChunkService) -> None:
        """limit 应限制返回数量。"""
        for i in range(10):
            await chunk_svc.save(_make_chunk(id=f"c{i}", user_id="user-1"))
        results = await chunk_svc.find_by_user("user-1", limit=3)
        assert len(results) <= 3


# ============================================================
# 7. _save_to_disk / _load_from_disk 测试
# ============================================================


class TestChunkPersistence:
    """测试磁盘持久化。"""

    @pytest.mark.asyncio
    async def test_保存后可从磁盘重新加载(self, temp_dir: str) -> None:
        """保存后重新创建 ChunkService 应能加载。"""
        svc1 = ChunkService(data_dir=temp_dir)
        chunk = _make_chunk(content="持久化测试")
        await svc1.save(chunk)

        svc2 = ChunkService(data_dir=temp_dir)
        loaded = await svc2.load("chunk-1")
        assert loaded is not None
        assert loaded.content == "持久化测试"

    @pytest.mark.asyncio
    async def test_保存后JSON内容正确(self, chunk_svc: ChunkService, temp_dir: str) -> None:
        """保存的 JSON 文件内容应正确。"""
        chunk = _make_chunk(keywords=["k1", "k2"])
        await chunk_svc.save(chunk)
        chunks_dir = Path(temp_dir) / "chunks"
        data = json.loads((chunks_dir / "chunk-1.json").read_text(encoding="utf-8"))
        assert data["id"] == "chunk-1"
        assert data["keywords"] == ["k1", "k2"]
        assert data["layer"] == "L1"

    @pytest.mark.asyncio
    async def test_删除后磁盘文件也被删除(self, chunk_svc: ChunkService, temp_dir: str) -> None:
        """删除后磁盘文件应被清理。"""
        chunk = _make_chunk()
        await chunk_svc.save(chunk)
        chunks_dir = Path(temp_dir) / "chunks"
        assert (chunks_dir / "chunk-1.json").exists()
        await chunk_svc.delete("chunk-1")
        assert not (chunks_dir / "chunk-1.json").exists()
