"""DiskFileStorage 单元测试（同步版本，兼容无 pytest-asyncio 环境）。

覆盖场景：
- save + load 往返（AttachmentInfo 对象）
- delete 删除文件
- exists 检查存在性
- 跨实例持久化（重启模拟）
- 加载不存在的文件返回 None
- 不支持的数据类型抛出 StorageError
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from multimodal.storage import DiskFileStorage, StorageError
from multimodal.types import AttachmentInfo, MediaType


def _async_run(coro):
    """安全执行 async 函数（兼容已有事件循环）。"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor() as pool:
        return pool.submit(asyncio.run, coro).result(timeout=10)


@pytest.fixture
def storage(tmp_path):
    """创建临时存储实例。"""
    return DiskFileStorage(base_dir=str(tmp_path))


@pytest.fixture
def sample_attachment():
    """创建测试用附件信息。"""
    return AttachmentInfo(
        file_id="test-file-001",
        filename="photo.jpg",
        mime_type="image/jpeg",
        size=1024,
        media_type=MediaType.IMAGE,
        base64_data="iVBORw0KGgo=",
    )


class TestDiskFileStorageSaveLoad:
    """save + load 往返测试。"""

    def test_save_and_load_attachment_info(self, storage, sample_attachment):
        """保存 AttachmentInfo 后加载，应得到等价对象。"""
        _async_run(storage.save("f1", sample_attachment))
        loaded = _async_run(storage.load("f1"))

        assert loaded is not None
        assert isinstance(loaded, AttachmentInfo)
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


class TestDiskFileStorageDelete:
    """delete 测试。"""

    def test_delete_existing_file(self, storage, sample_attachment):
        """删除已存在的文件返回 True。"""
        _async_run(storage.save("f1", sample_attachment))
        assert _async_run(storage.exists("f1"))

        deleted = _async_run(storage.delete("f1"))
        assert deleted is True
        assert not _async_run(storage.exists("f1"))
        assert _async_run(storage.load("f1")) is None

    def test_delete_nonexistent_returns_false(self, storage):
        """删除不存在的文件返回 False。"""
        assert _async_run(storage.delete("nonexistent")) is False


class TestDiskFileStorageExists:
    """exists 测试。"""

    def test_exists_after_save(self, storage, sample_attachment):
        """保存后 exists 返回 True。"""
        _async_run(storage.save("f1", sample_attachment))
        assert _async_run(storage.exists("f1")) is True

    def test_not_exists_before_save(self, storage):
        """未保存时 exists 返回 False。"""
        assert _async_run(storage.exists("f1")) is False


class TestDiskFileStoragePersistence:
    """跨实例持久化测试。"""

    def test_persistence_across_instances(self, tmp_path, sample_attachment):
        """新实例应能读取旧实例写入的数据（模拟重启）。"""
        s1 = DiskFileStorage(base_dir=str(tmp_path))
        _async_run(s1.save("persist-1", sample_attachment))

        # 模拟重启：创建新实例指向同一目录
        s2 = DiskFileStorage(base_dir=str(tmp_path))
        loaded = _async_run(s2.load("persist-1"))

        assert loaded is not None
        assert isinstance(loaded, AttachmentInfo)
        assert loaded.file_id == "test-file-001"

    def test_metadata_file_on_disk(self, tmp_path, sample_attachment):
        """元数据 JSON 文件确实写入磁盘。"""
        storage = DiskFileStorage(base_dir=str(tmp_path))
        _async_run(storage.save("f1", sample_attachment))

        meta_path = tmp_path / "f1.json"
        assert meta_path.exists()
        raw = json.loads(meta_path.read_text(encoding="utf-8"))
        assert raw["file_id"] == "test-file-001"
        assert raw["filename"] == "photo.jpg"


class TestDiskFileStorageErrorHandling:
    """错误处理测试。"""

    def test_unsupported_data_type_raises_error(self, storage):
        """不支持的数据类型应抛出 StorageError。"""
        with pytest.raises(StorageError):
            _async_run(storage.save("f1", 12345))

    def test_base_dir_auto_created(self, tmp_path):
        """构造时自动创建目录。"""
        new_dir = tmp_path / "deep" / "nested" / "storage"
        assert not new_dir.exists()

        DiskFileStorage(base_dir=str(new_dir))
        assert new_dir.exists()
