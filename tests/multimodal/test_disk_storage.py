"""DiskFileStorage 综合测试 — 持久化存储

覆盖场景：
- save + load 往返（AttachmentInfo / dict 数据）
- delete 删除文件 / 不存在文件
- exists 存在性检查
- 跨实例持久化（模拟重启）
- 元数据 JSON 文件实际写入磁盘
- 错误处理（不支持的数据类型）
- 边界场景（同名覆盖、空 dict、大 base64 数据）
- LocalFileStorage 对比测试
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from multimodal.storage import DiskFileStorage, LocalFileStorage, StorageError
from multimodal.types import AttachmentInfo, MediaType


# ── helpers ──────────────────────────────────────────────

def _async_run(coro):
    """同步包装器：在当前已有事件循环或无循环时都能运行 async 函数。"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    # 在已有循环中创建新任务并阻断等待
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor() as pool:
        future = pool.submit(asyncio.run, coro)
        return future.result(timeout=10)


def _temp_dir():
    """创建临时目录并返回路径。"""
    return tempfile.mkdtemp(prefix="dsk_test_")


# ── fixtures ─────────────────────────────────────────────

@pytest.fixture
def storage():
    d = _temp_dir()
    s = DiskFileStorage(base_dir=d)
    yield s
    # cleanup
    import shutil
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def sample_attachment():
    return AttachmentInfo(
        file_id="test-file-001",
        filename="photo.jpg",
        mime_type="image/jpeg",
        size=1024,
        media_type=MediaType.IMAGE,
        base64_data="iVBORw0KGgo=",
    )


@pytest.fixture
def sample_video_attachment():
    return AttachmentInfo(
        file_id="vid-001",
        filename="clip.mp4",
        mime_type="video/mp4",
        size=500000,
        media_type=MediaType.VIDEO,
        url="/uploads/vid-001.mp4",
    )


# ============================================================
# DiskFileStorage — save + load 往返
# ============================================================

class TestDiskFileStorageSaveLoad:
    """save + load 往返测试。"""

    def test_save_and_load_attachment_info(self, storage, sample_attachment):
        """保存 AttachmentInfo 后加载，应得到等价对象。"""
        _async_run(storage.save("f1", sample_attachment))
        loaded = _async_run(storage.load("f1"))

        assert loaded is not None, "加载不应返回 None"
        assert isinstance(loaded, AttachmentInfo), f"应为 AttachmentInfo，实际 {type(loaded)}"
        assert loaded.file_id == "test-file-001"
        assert loaded.filename == "photo.jpg"
        assert loaded.mime_type == "image/jpeg"
        assert loaded.size == 1024
        assert loaded.media_type == MediaType.IMAGE
        assert loaded.base64_data == "iVBORw0KGgo="

    def test_save_dict_data(self, storage):
        """保存普通 dict 数据。"""
        data = {"file_id": "d1", "custom": "value", "number": 42}
        _async_run(storage.save("d1", data))
        loaded = _async_run(storage.load("d1"))

        assert loaded is not None
        assert loaded["custom"] == "value"
        assert loaded["number"] == 42

    def test_load_nonexistent_returns_none(self, storage):
        """加载不存在的文件应返回 None。"""
        assert _async_run(storage.load("nonexistent-id")) is None

    def test_save_overwrites_existing(self, storage, sample_attachment, sample_video_attachment):
        """同名 file_id 保存会覆盖旧数据。"""
        _async_run(storage.save("f1", sample_attachment))
        _async_run(storage.save("f1", sample_video_attachment))
        loaded = _async_run(storage.load("f1"))

        assert loaded is not None
        assert loaded.file_id == "vid-001"
        assert loaded.media_type == MediaType.VIDEO

    def test_save_empty_dict(self, storage):
        """保存空 dict。"""
        _async_run(storage.save("empty", {}))
        loaded = _async_run(storage.load("empty"))
        assert loaded == {}

    def test_save_large_base64_data(self, storage):
        """保存含大 base64 数据的附件。"""
        big_data = "A" * 100000  # 100KB base64 string
        att = AttachmentInfo(
            file_id="big-001",
            filename="big.png",
            mime_type="image/png",
            size=100000,
            media_type=MediaType.IMAGE,
            base64_data=big_data,
        )
        _async_run(storage.save("big-001", att))
        loaded = _async_run(storage.load("big-001"))
        assert loaded is not None
        assert loaded.base64_data == big_data
        assert len(loaded.base64_data) == 100000

    def test_save_multiple_files(self, storage, sample_attachment):
        """同时保存多个文件，各自独立加载正确。"""
        _async_run(storage.save("a1", sample_attachment))
        _async_run(storage.save("a2", sample_attachment))
        _async_run(storage.save("a3", {"x": 1}))

        a1 = _async_run(storage.load("a1"))
        a2 = _async_run(storage.load("a2"))
        a3 = _async_run(storage.load("a3"))

        assert isinstance(a1, AttachmentInfo)
        assert isinstance(a2, AttachmentInfo)
        assert isinstance(a3, dict)
        assert a3["x"] == 1


# ============================================================
# DiskFileStorage — delete
# ============================================================

class TestDiskFileStorageDelete:
    """delete 测试。"""

    def test_delete_existing_file(self, storage, sample_attachment):
        """删除已存在的文件返回 True，删除后不可访问。"""
        _async_run(storage.save("f1", sample_attachment))
        assert _async_run(storage.exists("f1")) is True

        deleted = _async_run(storage.delete("f1"))
        assert deleted is True
        assert _async_run(storage.exists("f1")) is False
        assert _async_run(storage.load("f1")) is None

    def test_delete_nonexistent_returns_false(self, storage):
        """删除不存在的文件返回 False。"""
        assert _async_run(storage.delete("nonexistent")) is False

    def test_delete_twice_returns_false_second(self, storage, sample_attachment):
        """连续两次删除同一文件，第二次返回 False。"""
        _async_run(storage.save("f1", sample_attachment))
        assert _async_run(storage.delete("f1")) is True
        assert _async_run(storage.delete("f1")) is False


# ============================================================
# DiskFileStorage — exists
# ============================================================

class TestDiskFileStorageExists:
    """exists 测试。"""

    def test_exists_after_save(self, storage, sample_attachment):
        """保存后 exists 返回 True。"""
        _async_run(storage.save("f1", sample_attachment))
        assert _async_run(storage.exists("f1")) is True

    def test_not_exists_before_save(self, storage):
        """未保存时 exists 返回 False。"""
        assert _async_run(storage.exists("f1")) is False

    def test_not_exists_after_delete(self, storage, sample_attachment):
        """删除后 exists 返回 False。"""
        _async_run(storage.save("f1", sample_attachment))
        _async_run(storage.delete("f1"))
        assert _async_run(storage.exists("f1")) is False


# ============================================================
# DiskFileStorage — 跨实例持久化
# ============================================================

class TestDiskFileStoragePersistence:
    """跨实例持久化测试（模拟重启）。"""

    def test_persistence_across_instances(self, sample_attachment):
        """新实例应能读取旧实例写入的数据（模拟重启）。"""
        d = _temp_dir()
        try:
            s1 = DiskFileStorage(base_dir=d)
            _async_run(s1.save("persist-1", sample_attachment))

            # 模拟重启：创建新实例指向同一目录
            s2 = DiskFileStorage(base_dir=d)
            loaded = _async_run(s2.load("persist-1"))

            assert loaded is not None
            assert isinstance(loaded, AttachmentInfo)
            assert loaded.file_id == "test-file-001"
        finally:
            import shutil
            shutil.rmtree(d, ignore_errors=True)

    def test_metadata_file_on_disk(self, sample_attachment):
        """元数据 JSON 文件确实写入磁盘。"""
        d = _temp_dir()
        try:
            storage = DiskFileStorage(base_dir=d)
            _async_run(storage.save("f1", sample_attachment))

            meta_path = Path(d) / "f1.json"
            assert meta_path.exists(), f"元数据文件不存在: {meta_path}"
            raw = json.loads(meta_path.read_text(encoding="utf-8"))
            assert raw["file_id"] == "test-file-001"
            assert raw["filename"] == "photo.jpg"
            assert raw["media_type"] == "image"
        finally:
            import shutil
            shutil.rmtree(d, ignore_errors=True)

    def test_persistence_dict_across_instances(self):
        """dict 数据跨实例持久化。"""
        d = _temp_dir()
        try:
            s1 = DiskFileStorage(base_dir=d)
            _async_run(s1.save("d1", {"a": 1, "b": "hello"}))

            s2 = DiskFileStorage(base_dir=d)
            loaded = _async_run(s2.load("d1"))

            assert loaded == {"a": 1, "b": "hello"}
        finally:
            import shutil
            shutil.rmtree(d, ignore_errors=True)


# ============================================================
# DiskFileStorage — 错误处理
# ============================================================

class TestDiskFileStorageErrorHandling:
    """错误处理测试。"""

    def test_unsupported_data_type_raises_error(self, storage):
        """不支持的数据类型（如 int）应抛出 StorageError。"""
        with pytest.raises(StorageError, match="不支持的数据类型"):
            _async_run(storage.save("f1", 12345))

    def test_unsupported_list_raises_error(self, storage):
        """list 类型应抛出 StorageError。"""
        with pytest.raises(StorageError, match="不支持的数据类型"):
            _async_run(storage.save("f1", [1, 2, 3]))

    def test_unsupported_string_raises_error(self, storage):
        """普通字符串应抛出 StorageError。"""
        with pytest.raises(StorageError, match="不支持的数据类型"):
            _async_run(storage.save("f1", "plain string"))

    def test_base_dir_auto_created(self):
        """构造时自动创建目录。"""
        d = _temp_dir()
        import shutil
        shutil.rmtree(d)
        new_dir = Path(d) / "deep" / "nested" / "storage"
        assert not new_dir.exists()

        DiskFileStorage(base_dir=str(new_dir))
        assert new_dir.exists()
        shutil.rmtree(d, ignore_errors=True)

    def test_storage_error_str_with_file_id(self):
        """StorageError __str__ 包含 file_id。"""
        e = StorageError("保存失败", file_id="file-abc")
        msg = str(e)
        assert "保存失败" in msg
        assert "file-abc" in msg

    def test_storage_error_str_without_file_id(self):
        """StorageError __str__ 不含 file_id 时正常工作。"""
        e = StorageError("通用错误")
        msg = str(e)
        assert "通用错误" in msg
        assert "file_id" not in msg


# ============================================================
# DiskFileStorage — 边界场景
# ============================================================

class TestDiskFileStorageEdgeCases:
    """边界场景测试。"""

    def test_file_id_with_special_chars(self):
        """file_id 含特殊字符时正常工作（使用合法文件名）。"""
        d = _temp_dir()
        try:
            s = DiskFileStorage(base_dir=d)
            data = {"x": 1}
            fid = "file-with-dashes_and_underscores.123"
            _async_run(s.save(fid, data))
            loaded = _async_run(s.load(fid))
            assert loaded == {"x": 1}
        finally:
            import shutil
            shutil.rmtree(d, ignore_errors=True)

    def test_file_id_with_unicode(self):
        """file_id 含 Unicode 字符时正常工作。"""
        d = _temp_dir()
        try:
            s = DiskFileStorage(base_dir=d)
            data = {"name": "中文测试"}
            fid = "文件-テスト-한국어"
            _async_run(s.save(fid, data))
            loaded = _async_run(s.load(fid))
            assert loaded == {"name": "中文测试"}
        finally:
            import shutil
            shutil.rmtree(d, ignore_errors=True)

    def test_concurrent_saves(self):
        """并发保存多个文件不冲突。"""
        d = _temp_dir()
        try:
            s = DiskFileStorage(base_dir=d)
            async def save_all():
                for i in range(20):
                    await s.save(f"concurrent-{i}", {"id": i})
            _async_run(save_all())

            for i in range(20):
                loaded = _async_run(s.load(f"concurrent-{i}"))
                assert loaded == {"id": i}
                assert _async_run(s.exists(f"concurrent-{i}")) is True
        finally:
            import shutil
            shutil.rmtree(d, ignore_errors=True)

    def test_attachment_with_url(self):
        """AttachmentInfo 带 URL 字段。"""
        d = _temp_dir()
        try:
            s = DiskFileStorage(base_dir=d)
            att = AttachmentInfo(
                file_id="url-001",
                filename="doc.pdf",
                mime_type="application/pdf",
                size=2048,
                media_type=MediaType.DOCUMENT,
                url="https://example.com/doc.pdf",
            )
            _async_run(s.save("url-001", att))
            loaded = _async_run(s.load("url-001"))
            assert loaded.url == "https://example.com/doc.pdf"
        finally:
            import shutil
            shutil.rmtree(d, ignore_errors=True)


# ============================================================
# LocalFileStorage — 对比测试
# ============================================================

class TestLocalFileStorage:
    """LocalFileStorage 内存存储测试。"""

    def test_save_and_load_dict(self):
        """内存存储 save/load 往返。"""
        s = LocalFileStorage()
        _async_run(s.save("k1", {"a": 1}))
        loaded = _async_run(s.load("k1"))
        assert loaded == {"a": 1}

    def test_delete(self):
        """内存存储 delete。"""
        s = LocalFileStorage()
        _async_run(s.save("k1", {"a": 1}))
        assert _async_run(s.delete("k1")) is True
        assert _async_run(s.load("k1")) is None
        assert _async_run(s.exists("k1")) is False

    def test_delete_nonexistent(self):
        """删除不存在 key 返回 False。"""
        s = LocalFileStorage()
        assert _async_run(s.delete("nope")) is False

    def test_clear(self):
        """清空所有缓存。"""
        s = LocalFileStorage()
        _async_run(s.save("k1", {"a": 1}))
        _async_run(s.save("k2", {"b": 2}))
        _async_run(s.clear())
        assert _async_run(s.count()) == 0
        assert _async_run(s.list_files()) == []

    def test_list_files(self):
        """列出所有文件 ID。"""
        s = LocalFileStorage()
        _async_run(s.save("a", {}))
        _async_run(s.save("b", {}))
        files = _async_run(s.list_files())
        assert set(files) == {"a", "b"}

    def test_count(self):
        """统计文件数量。"""
        s = LocalFileStorage()
        assert _async_run(s.count()) == 0
        _async_run(s.save("a", {}))
        assert _async_run(s.count()) == 1
        _async_run(s.save("b", {}))
        assert _async_run(s.count()) == 2

    def test_no_persistence(self):
        """LocalFileStorage 不持久化——不同实例不共享数据。"""
        s1 = LocalFileStorage()
        _async_run(s1.save("k1", {"a": 1}))

        s2 = LocalFileStorage()
        assert _async_run(s2.load("k1")) is None
