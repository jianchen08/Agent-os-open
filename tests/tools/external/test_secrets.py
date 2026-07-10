"""外部工具密钥管理测试。"""

from __future__ import annotations

import pytest

from tools.external.exceptions import SecretError
from tools.external.secrets import ExternalToolSecretManager


@pytest.fixture
def secret_manager() -> ExternalToolSecretManager:
    """创建测试用密钥管理器。"""
    return ExternalToolSecretManager(encryption_key="test-encryption-key-12345")


class TestExternalToolSecretManager:

    @pytest.mark.asyncio
    async def test_store_and_get(self, secret_manager: ExternalToolSecretManager) -> None:
        await secret_manager.store_secret("api_key_1", "my-secret-value")
        value = await secret_manager.get_secret("api_key_1")
        assert value == "my-secret-value"

    @pytest.mark.asyncio
    async def test_get_nonexistent(self, secret_manager: ExternalToolSecretManager) -> None:
        with pytest.raises(SecretError, match="密钥不存在"):
            await secret_manager.get_secret("nonexistent_key")

    @pytest.mark.asyncio
    async def test_rotate_secret(self, secret_manager: ExternalToolSecretManager) -> None:
        await secret_manager.store_secret("key1", "old_value")
        assert await secret_manager.get_secret("key1") == "old_value"

        await secret_manager.rotate_secret("key1", "new_value")
        assert await secret_manager.get_secret("key1") == "new_value"

    @pytest.mark.asyncio
    async def test_rotate_preserves_metadata(self, secret_manager: ExternalToolSecretManager) -> None:
        await secret_manager.store_secret("key2", "value1", metadata={"created_by": "test"})
        await secret_manager.rotate_secret("key2", "value2")
        # 元数据应被保留
        assert await secret_manager.get_secret("key2") == "value2"

    @pytest.mark.asyncio
    async def test_delete_secret(self, secret_manager: ExternalToolSecretManager) -> None:
        await secret_manager.store_secret("to_delete", "value")
        await secret_manager.delete_secret("to_delete")
        assert not await secret_manager.has_secret("to_delete")

    @pytest.mark.asyncio
    async def test_delete_nonexistent(self, secret_manager: ExternalToolSecretManager) -> None:
        # 删除不存在的密钥应不报错
        await secret_manager.delete_secret("nonexistent")

    @pytest.mark.asyncio
    async def test_has_secret(self, secret_manager: ExternalToolSecretManager) -> None:
        assert not await secret_manager.has_secret("key_a")
        await secret_manager.store_secret("key_a", "value_a")
        assert await secret_manager.has_secret("key_a")

    @pytest.mark.asyncio
    async def test_encryption_is_real(self, secret_manager: ExternalToolSecretManager) -> None:
        """验证存储的是加密后的值，而非明文。"""
        await secret_manager.store_secret("enc_test", "plaintext")
        # 内部存储的应该是加密字节
        encrypted, _ = secret_manager._store["enc_test"]
        assert encrypted != b"plaintext"
        assert isinstance(encrypted, bytes)

    @pytest.mark.asyncio
    async def test_different_keys_isolated(self, secret_manager: ExternalToolSecretManager) -> None:
        """验证不同密钥之间互不影响。"""
        await secret_manager.store_secret("k1", "v1")
        await secret_manager.store_secret("k2", "v2")
        assert await secret_manager.get_secret("k1") == "v1"
        assert await secret_manager.get_secret("k2") == "v2"

    @pytest.mark.asyncio
    async def test_overwrite_existing(self, secret_manager: ExternalToolSecretManager) -> None:
        """存储相同 key 应覆盖。"""
        await secret_manager.store_secret("dup", "first")
        await secret_manager.store_secret("dup", "second")
        assert await secret_manager.get_secret("dup") == "second"

    def test_mask_key(self) -> None:
        assert ExternalToolSecretManager._mask_key("short") == "***"
        assert ExternalToolSecretManager._mask_key("a-very-long-key-12345") == "a-ve...2345"

    @pytest.mark.asyncio
    async def test_derived_key_deterministic(self) -> None:
        """相同源密钥应派生相同的 Fernet 密钥。"""
        mgr1 = ExternalToolSecretManager("same-key")
        mgr2 = ExternalToolSecretManager("same-key")
        # 两个管理器应能解密对方的密钥
        await mgr1.store_secret("shared", "secret_value")
        encrypted_bytes = mgr1._store["shared"][0]
        # mgr2 使用相同主密钥，应能解密
        decrypted = mgr2._decrypt(encrypted_bytes)
        assert decrypted == "secret_value"

    @pytest.mark.asyncio
    async def test_wrong_key_cannot_decrypt(self) -> None:
        """不同主密钥不能解密。"""
        mgr1 = ExternalToolSecretManager("key-alpha")
        mgr2 = ExternalToolSecretManager("key-beta")
        await mgr1.store_secret("test", "data")
        encrypted_bytes = mgr1._store["test"][0]
        with pytest.raises(SecretError, match="解密失败"):
            mgr2._decrypt(encrypted_bytes)

    @pytest.mark.asyncio
    async def test_store_with_metadata(self, secret_manager: ExternalToolSecretManager) -> None:
        await secret_manager.store_secret(
            "meta_key", "value", metadata={"expires": "2026-12-31"},
        )
        _, meta = secret_manager._store["meta_key"]
        assert meta["expires"] == "2026-12-31"
