"""外部工具密钥管理。

暴露接口：
- ExternalToolSecretManager：基于 Fernet 对称加密的密钥安全存储
"""

from __future__ import annotations

import base64
import hashlib
import logging
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from tools.external.exceptions import SecretError
from tools.external.interfaces import ISecretManager

logger = logging.getLogger(__name__)


class ExternalToolSecretManager(ISecretManager):
    """密钥管理器。

    职责：
    - 基于 settings.jwt_secret_key 派生 Fernet 密钥进行加密存储
    - 密钥读取时解密返回
    - 日志中对密钥值脱敏
    - 支持密钥验证和轮换

    注意：密钥存储在内存中，生产环境应替换为持久化后端。
    """

    def __init__(self, encryption_key: str) -> None:
        """初始化密钥管理器。

        Args:
            encryption_key: 加密主密钥（通常使用 settings.jwt_secret_key）
        """
        self._fernet = self._derive_fernet_key(encryption_key)
        self._store: dict[str, tuple[bytes, dict[str, Any]]] = {}
        self._logger = logging.getLogger(f"{__name__}")

    @staticmethod
    def _derive_fernet_key(source: str) -> Fernet:
        """从源密钥派生 Fernet 兼容的加密密钥。

        Args:
            source: 源密钥字符串

        Returns:
            Fernet 加密器实例
        """
        # 使用 SHA-256 哈希确保密钥长度为 32 字节
        digest = hashlib.sha256(source.encode()).digest()
        # Base64 编码为 Fernet 兼容格式（32 字节）
        key = base64.urlsafe_b64encode(digest)
        return Fernet(key)

    def _encrypt(self, value: str) -> bytes:
        """加密值。

        Args:
            value: 明文值

        Returns:
            加密后的字节
        """
        return self._fernet.encrypt(value.encode())

    def _decrypt(self, encrypted: bytes) -> str:
        """解密值。

        Args:
            encrypted: 加密字节

        Returns:
            明文字符串

        Raises:
            SecretError: 解密失败
        """
        try:
            return self._fernet.decrypt(encrypted).decode()
        except InvalidToken as e:
            raise SecretError(
                message="密钥解密失败，可能密钥已损坏或主密钥已变更",
                cause=e,
            ) from e

    @staticmethod
    def _mask_key(key: str) -> str:
        """脱敏密钥用于日志输出。

        Args:
            key: 原始密钥

        Returns:
            脱敏后的字符串
        """
        if len(key) <= 8:
            return "***"
        return f"{key[:4]}...{key[-4:]}"

    async def store_secret(
        self,
        key: str,
        value: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """存储密钥（加密存储）。

        Args:
            key: 密钥标识
            value: 密钥明文值
            metadata: 元数据
        """
        try:
            encrypted = self._encrypt(value)
            self._store[key] = (encrypted, metadata or {})
            self._logger.info(
                "密钥已存储 | key=%s",
                self._mask_key(key),
            )
        except Exception as e:
            raise SecretError(
                message=f"密钥存储失败: {e}",
                secret_key=key,
                cause=e,
            ) from e

    async def get_secret(self, key: str) -> str:
        """获取密钥（解密返回）。

        Args:
            key: 密钥标识

        Returns:
            密钥明文值

        Raises:
            SecretError: 密钥不存在或解密失败
        """
        if key not in self._store:
            raise SecretError(
                message=f"密钥不存在: {self._mask_key(key)}",
                secret_key=key,
            )

        encrypted, _ = self._store[key]
        return self._decrypt(encrypted)

    async def rotate_secret(self, key: str, new_value: str) -> None:
        """轮换密钥（更新值，保留元数据）。

        Args:
            key: 密钥标识
            new_value: 新密钥值
        """
        existing_meta: dict[str, Any] = {}
        if key in self._store:
            _, existing_meta = self._store[key]

        await self.store_secret(key, new_value, existing_meta)
        self._logger.info("密钥已轮换 | key=%s", self._mask_key(key))

    async def delete_secret(self, key: str) -> None:
        """删除密钥。

        Args:
            key: 密钥标识
        """
        if key in self._store:
            del self._store[key]
            self._logger.info("密钥已删除 | key=%s", self._mask_key(key))

    async def has_secret(self, key: str) -> bool:
        """检查密钥是否存在。

        Args:
            key: 密钥标识

        Returns:
            是否存在
        """
        return key in self._store
