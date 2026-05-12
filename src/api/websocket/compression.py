"""
WebSocket 消息压缩模块

使用 gzip 压缩大消息，减少带宽占用
"""

import gzip
import json
import logging
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class CompressionConfig:
    """压缩配置"""

    threshold: int = 1024  # 压缩阈值（字节）
    level: int = 6  # 压缩级别 (1-9)
    enabled: bool = False  # 是否启用压缩（禁用以避免前端解压问题）


@dataclass
class CompressionResult:
    """压缩结果"""

    compressed: bool  # 是否已压缩
    data: bytes  # 压缩后的数据
    original_size: int  # 原始大小
    compressed_size: int  # 压缩后大小
    compression_ratio: float  # 压缩率 (0-1)
    compression_time: float  # 压缩耗时（秒）


@dataclass
class DecompressionResult:
    """解压结果"""

    data: str  # 解压后的数据
    compressed_size: int  # 压缩大小
    decompressed_size: int  # 解压后大小
    decompression_time: float  # 解压耗时（秒）


class MessageCompressor:
    """
    WebSocket 消息压缩器

    功能：
    1. 自动判断是否需要压缩
    2. 使用 gzip 压缩大消息
    3. 添加压缩标记
    4. 性能统计
    """

    # 压缩标记
    COMPRESSED_MARKER = b"\x01"
    UNCOMPRESSED_MARKER = b"\x00"

    def __init__(self, config: CompressionConfig | None = None):
        """
        初始化压缩器

        Args:
            config: 压缩配置
        """
        self.config = config or CompressionConfig()

        # 统计信息
        self.total_messages = 0
        self.compressed_messages = 0
        self.total_original_size = 0
        self.total_compressed_size = 0
        self.total_compression_time = 0.0

        logger.info(f"[MessageCompressor] 初始化完成，配置: {self.config}")

    def compress(self, message: dict[str, Any]) -> CompressionResult:
        """
        压缩消息

        Args:
            message: 要压缩的消息字典

        Returns:
            压缩结果
        """
        start_time = time.time()

        # 序列化消息
        json_str = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
        original_data = json_str.encode("utf-8")
        original_size = len(original_data)

        self.total_messages += 1
        self.total_original_size += original_size

        # 检查是否需要压缩
        if not self.config.enabled or original_size < self.config.threshold:
            # 不压缩，添加未压缩标记
            result_data = self.UNCOMPRESSED_MARKER + original_data
            compression_time = time.time() - start_time

            return CompressionResult(
                compressed=False,
                data=result_data,
                original_size=original_size,
                compressed_size=len(result_data),
                compression_ratio=0.0,
                compression_time=compression_time,
            )

        try:
            # 使用 gzip 压缩
            compressed_data = gzip.compress(
                original_data, compresslevel=self.config.level
            )

            # 添加压缩标记
            result_data = self.COMPRESSED_MARKER + compressed_data
            compression_time = time.time() - start_time

            # 计算压缩率
            compression_ratio = 1.0 - (len(result_data) / original_size)

            # 更新统计
            self.compressed_messages += 1
            self.total_compressed_size += len(result_data)
            self.total_compression_time += compression_time

            logger.debug(
                f"[MessageCompressor] 压缩完成: {original_size} -> {len(result_data)} 字节 "
                f"({compression_ratio:.1%} 节省, {compression_time * 1000:.2f}ms)"
            )

            return CompressionResult(
                compressed=True,
                data=result_data,
                original_size=original_size,
                compressed_size=len(result_data),
                compression_ratio=compression_ratio,
                compression_time=compression_time,
            )

        except Exception as e:
            logger.error(f"[MessageCompressor] 压缩失败: {e}")

            # 压缩失败，返回未压缩数据
            result_data = self.UNCOMPRESSED_MARKER + original_data
            compression_time = time.time() - start_time

            return CompressionResult(
                compressed=False,
                data=result_data,
                original_size=original_size,
                compressed_size=len(result_data),
                compression_ratio=0.0,
                compression_time=compression_time,
            )

    def decompress(self, data: bytes) -> DecompressionResult:
        """
        解压消息

        Args:
            data: 压缩的数据

        Returns:
            解压结果

        Raises:
            ValueError: 数据格式错误
            Exception: 解压失败
        """
        start_time = time.time()
        compressed_size = len(data)

        if len(data) == 0:
            raise ValueError("数据为空")

        # 检查压缩标记
        marker = data[:1]
        payload = data[1:]

        if marker == self.UNCOMPRESSED_MARKER:
            # 未压缩数据，直接解码
            try:
                decompressed_str = payload.decode("utf-8")
                decompression_time = time.time() - start_time

                return DecompressionResult(
                    data=decompressed_str,
                    compressed_size=compressed_size,
                    decompressed_size=len(payload),
                    decompression_time=decompression_time,
                )
            except UnicodeDecodeError as e:
                raise ValueError(f"UTF-8 解码失败: {e}")

        elif marker == self.COMPRESSED_MARKER:
            # 压缩数据，需要解压
            try:
                decompressed_data = gzip.decompress(payload)
                decompressed_str = decompressed_data.decode("utf-8")
                decompression_time = time.time() - start_time

                logger.debug(
                    f"[MessageCompressor] 解压完成: {compressed_size} -> {len(decompressed_data)} 字节 "
                    f"({decompression_time * 1000:.2f}ms)"
                )

                return DecompressionResult(
                    data=decompressed_str,
                    compressed_size=compressed_size,
                    decompressed_size=len(decompressed_data),
                    decompression_time=decompression_time,
                )

            except gzip.BadGzipFile as e:
                raise Exception(f"gzip 解压失败: {e}")
            except UnicodeDecodeError as e:
                raise Exception(f"UTF-8 解码失败: {e}")
        else:
            raise ValueError(f"未知的压缩标记: {marker}")

    def is_compressed(self, data: bytes) -> bool:
        """
        检查数据是否已压缩

        Args:
            data: 数据

        Returns:
            是否已压缩
        """
        return len(data) > 0 and data[:1] == self.COMPRESSED_MARKER

    def should_compress(self, message: dict[str, Any]) -> bool:
        """
        判断是否应该压缩消息

        Args:
            message: 消息字典

        Returns:
            是否应该压缩
        """
        if not self.config.enabled:
            return False

        json_str = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
        size = len(json_str.encode("utf-8"))

        return size >= self.config.threshold

    def get_message_size(self, message: dict[str, Any]) -> int:
        """
        获取消息大小（字节）

        Args:
            message: 消息字典

        Returns:
            消息大小
        """
        json_str = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
        return len(json_str.encode("utf-8"))

    def estimate_compressed_size(self, message: dict[str, Any]) -> int:
        """
        估算压缩后大小

        Args:
            message: 消息字典

        Returns:
            估算的压缩后大小
        """
        original_size = self.get_message_size(message)

        if original_size < self.config.threshold:
            return original_size + 1  # 未压缩标记

        # 简单估算：假设压缩率为 60%
        return int(original_size * 0.6) + 1  # 压缩标记

    def update_config(self, config: CompressionConfig) -> None:
        """
        更新配置

        Args:
            config: 新配置
        """
        self.config = config
        logger.info(f"[MessageCompressor] 配置已更新: {self.config}")

    def get_config(self) -> CompressionConfig:
        """获取当前配置"""
        return self.config

    def get_stats(self) -> dict[str, Any]:
        """
        获取压缩统计信息

        Returns:
            统计信息字典
        """
        if self.total_messages == 0:
            return {
                "total_messages": 0,
                "compressed_messages": 0,
                "compression_rate": 0.0,
                "total_original_size": 0,
                "total_compressed_size": 0,
                "overall_compression_ratio": 0.0,
                "average_compression_time": 0.0,
                "bandwidth_saved": 0,
            }

        compression_rate = self.compressed_messages / self.total_messages
        overall_compression_ratio = 1.0 - (
            self.total_compressed_size / self.total_original_size
        )
        average_compression_time = self.total_compression_time / self.total_messages
        bandwidth_saved = self.total_original_size - self.total_compressed_size

        return {
            "total_messages": self.total_messages,
            "compressed_messages": self.compressed_messages,
            "compression_rate": compression_rate,
            "total_original_size": self.total_original_size,
            "total_compressed_size": self.total_compressed_size,
            "overall_compression_ratio": overall_compression_ratio,
            "average_compression_time": average_compression_time,
            "bandwidth_saved": bandwidth_saved,
        }

    def reset_stats(self) -> None:
        """重置统计信息"""
        self.total_messages = 0
        self.compressed_messages = 0
        self.total_original_size = 0
        self.total_compressed_size = 0
        self.total_compression_time = 0.0

        logger.info("[MessageCompressor] 统计信息已重置")


# 全局压缩器实例
_global_compressor: MessageCompressor | None = None


def get_message_compressor() -> MessageCompressor:
    """获取全局消息压缩器实例"""
    global _global_compressor
    if _global_compressor is None:
        _global_compressor = MessageCompressor()
    return _global_compressor


def init_message_compressor(
    config: CompressionConfig | None = None,
) -> MessageCompressor:
    """
    初始化全局消息压缩器

    Args:
        config: 压缩配置

    Returns:
        压缩器实例
    """
    global _global_compressor
    _global_compressor = MessageCompressor(config)
    return _global_compressor


def compress_message(message: dict[str, Any]) -> CompressionResult:
    """
    压缩消息（便捷函数）

    Args:
        message: 消息字典

    Returns:
        压缩结果
    """
    return get_message_compressor().compress(message)


def decompress_message(data: bytes) -> dict[str, Any]:
    """
    解压消息（便捷函数）

    Args:
        data: 压缩数据

    Returns:
        解压后的消息字典
    """
    result = get_message_compressor().decompress(data)
    return json.loads(result.data)


def should_compress_message(message: dict[str, Any]) -> bool:
    """
    判断是否应该压缩消息（便捷函数）

    Args:
        message: 消息字典

    Returns:
        是否应该压缩
    """
    return get_message_compressor().should_compress(message)


def get_compression_stats() -> dict[str, Any]:
    """
    获取压缩统计信息（便捷函数）

    Returns:
        统计信息字典
    """
    return get_message_compressor().get_stats()
