"""外部工具密钥管理器测试。"""

from __future__ import annotations

from typing import Any

import pytest

from tools.external.exceptions import SecretError
from tools.external.secrets import ExternalToolSecretManager


# ════════════════════════════════════════════
# 辅助 fixtures
# ════════════════════════════════════════════


@pytest.fixture
def secret_mgr() -> ExternalToolSecretManager:
    """密钥管理器实例。"""
    return ExternalToolSecretManager("test_encryption_key_12345")


# ════════════════════════════════════════════
# 密钥加密存储与解密读取
# ════════════════════════════════════════════


class TestStoreAndGet:
    """密钥存储和读取测试。"""

    @pytest.mark.asyncio
    async def test_store_and_get(self, secret_mgr: ExternalToolSecretManager) -> None:
        """存储后可以解密读取。"""
        await secret_mgr.store_secret("api_key_1", "super_secret_value")
        result = await secret_mgr.get_secret("api_key_1")
        assert result == "super_secret_value"

    @pytest.mark.asyncio
    async def test_store_with_metadata(self, secret_mgr: ExternalToolSecretManager) -> None:
        """存储时附加元数据。"""
        await secret_mgr.store_secret(
            "key_with_meta", "value", {"description": "test key", "expires": "2026-12-31"}
        )
        result = await secret_mgr.get_secret("key_with_meta")
        assert result == "value"

    @pytest.mark.asyncio
    async def test_get_nonexistent_raises(self, secret_mgr: ExternalToolSecretManager) -> None:
        """获取不存在的密钥抛出 SecretError。"""
        with pytest.raises(SecretError) as exc_info:
            await secret_mgr.get_secret("nonexistent")
        assert "不存在" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_store_multiple_keys(self, secret_mgr: ExternalToolSecretManager) -> None:
        """存储多个密钥。"""
        await secret_mgr.store_secret("key1", "val1")
        await secret_mgr.store_secret("key2", "val2")
        assert await secret_mgr.get_secret("key1") == "val1"
        assert await secret_mgr.get_secret("key2") == "val2"

    @pytest.mark.asyncio
    async def test_overwrite_existing(self, secret_mgr: ExternalToolSecretManager) -> None:
        """覆盖已存在的密钥。"""
        await secret_mgr.store_secret("key", "old_value")
        await secret_mgr.store_secret("key", "new_value")
        assert await secret_mgr.get_secret("key") == "new_value"


# ════════════════════════════════════════════
# 密钥轮换
# ════════════════════════════════════════════


class TestRotateSecret:
    """密钥轮换测试。"""

    @pytest.mark.asyncio
    async def test_rotate_updates_value(self, secret_mgr: ExternalToolSecretManager) -> None:
        """轮换后新值生效。"""
        await secret_mgr.store_secret("rot_key", "old")
        await secret_mgr.rotate_secret("rot_key", "new")
        assert await secret_mgr.get_secret("rot_key") == "new"

    @pytest.mark.asyncio
    async def test_rotate_preserves_metadata(self, secret_mgr: ExternalToolSecretManager) -> None:
        """轮换保留元数据。"""
        await secret_mgr.store_secret("key", "val", {"created_by": "admin"})
        await secret_mgr.rotate_secret("key", "new_val")
        assert await secret_mgr.get_secret("key") == "new_val"
        # 元数据保留在内部存储中
        _, meta = secret_mgr._store["key"]
        assert meta["created_by"] == "admin"

    @pytest.mark.asyncio
    async def test_rotate_nonexistent_creates(self, secret_mgr: ExternalToolSecretManager) -> None:
        """轮换不存在的密钥会创建它。"""
        await secret_mgr.rotate_secret("new_key", "value")
        assert await secret_mgr.get_secret("new_key") == "value"


# ════════════════════════════════════════════
# 密钥删除
# ════════════════════════════════════════════


class TestDeleteSecret:
    """密钥删除测试。"""

    @pytest.mark.asyncio
    async def test_delete_existing(self, secret_mgr: ExternalToolSecretManager) -> None:
        """删除已存在的密钥。"""
        await secret_mgr.store_secret("del_key", "val")
        await secret_mgr.delete_secret("del_key")
        assert not await secret_mgr.has_secret("del_key")

    @pytest.mark.asyncio
    async def test_delete_nonexistent_no_error(self, secret_mgr: ExternalToolSecretManager) -> None:
        """删除不存在的密钥不报错。"""
        await secret_mgr.delete_secret("nonexistent")  # 不应抛异常

    @pytest.mark.asyncio
    async def test_get_after_delete_raises(self, secret_mgr: ExternalToolSecretManager) -> None:
        """删除后获取抛出 SecretError。"""
        await secret_mgr.store_secret("temp", "val")
        await secret_mgr.delete_secret("temp")
        with pytest.raises(SecretError):
            await secret_mgr.get_secret("temp")


# ════════════════════════════════════════════
# 密钥存在检查
# ════════════════════════════════════════════


class TestHasSecret:
    """密钥存在检查测试。"""

    @pytest.mark.asyncio
    async def test_has_secret_true(self, secret_mgr: ExternalToolSecretManager) -> None:
        """存在的密钥返回 True。"""
        await secret_mgr.store_secret("exists", "val")
        assert await secret_mgr.has_secret("exists") is True

    @pytest.mark.asyncio
    async def test_has_secret_false(self, secret_mgr: ExternalToolSecretManager) -> None:
        """不存在的密钥返回 False。"""
        assert await secret_mgr.has_secret("nope") is False


# ════════════════════════════════════════════
# 加密安全性
# ════════════════════════════════════════════


class TestEncryption:
    """加密安全性测试。"""

    @pytest.mark.asyncio
    async def test_stored_value_is_encrypted(self, secret_mgr: ExternalToolSecretManager) -> None:
        """存储的值是加密的（不是明文）。"""
        await secret_mgr.store_secret("enc_test", "plaintext_value")
        encrypted, _ = secret_mgr._store["enc_test"]
        assert encrypted != b"plaintext_value"
        assert isinstance(encrypted, bytes)

    @pytest.mark.asyncio
    async def test_different_keys_different_encryption(self, secret_mgr: ExternalToolSecretManager) -> None:
        """相同值不同密钥名产生不同密文。"""
        await secret_mgr.store_secret("k1", "same_val")
        await secret_mgr.store_secret("k2", "same_val")
        enc1, _ = secret_mgr._store["k1"]
        enc2, _ = secret_mgr._store["k2"]
        # Fernet 每次加密产生不同密文（含随机 IV）
        assert enc1 != enc2

    def test_derive_fernet_key_deterministic(self) -> None:
        """相同源密钥派生相同的 Fernet 密钥。"""
        f1 = ExternalToolSecretManager._derive_fernet_key("my_key")
        f2 = ExternalToolSecretManager._derive_fernet_key("my_key")
        # 两个 Fernet 实例使用相同密钥，可以互相解密
        token = f1.encrypt(b"test")
        assert f2.decrypt(token) == b"test"


# ════════════════════════════════════════════
# 日志脱敏
# ════════════════════════════════════════════


class TestMaskKey:
    """密钥脱敏测试。"""

    def test_mask_long_key(self) -> None:
        """长密钥保留前4和后4位。"""
        result = ExternalToolSecretManager._mask_key("abcdefghij")
        assert result == "abcd...ghij"

    def test_mask_short_key(self) -> None:
        """短密钥（<=8）返回 ***。"""
        result = ExternalToolSecretManager._mask_key("short")
        assert result == "***"

    def test_mask_empty_key(self) -> None:
        """空密钥返回 ***。"""
        result = ExternalToolSecretManager._mask_key("")
        assert result == "***"

    def test_mask_exact_8_chars(self) -> None:
        """恰好 8 字符也脱敏。"""
        result = ExternalToolSecretManager._mask_key("12345678")
        assert result == "***"  # len<=8 returns ***

    def test_mask_9_chars(self) -> None:
        """9 字符保留前后4位。"""
        result = ExternalToolSecretManager._mask_key("123456789")
        assert result == "1234...6789"


# ════════════════════════════════════════════
# 不同加密主密钥
# ════════════════════════════════════════════


class TestDifferentEncryptionKeys:
    """不同加密主密钥隔离测试。"""

    @pytest.mark.asyncio
    async def test_different_master_key_cannot_decrypt(self) -> None:
        """不同主密钥无法解密另一密钥管理器加密的数据。"""
        mgr1 = ExternalToolSecretManager("master_key_1")
        mgr2 = ExternalToolSecretManager("master_key_2")
        await mgr1.store_secret("shared_key", "secret_data")
        # 获取 mgr1 的加密数据
        encrypted, meta = mgr1._store["shared_key"]
        # 将密文放入 mgr2
        mgr2._store["shared_key"] = (encrypted, meta)
        # 尝试用 mgr2 解密应失败
        with pytest.raises(SecretError):
            await mgr2.get_secret("shared_key")
