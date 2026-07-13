"""管道检查点管理器。

负责管道运行时状态的快照持久化，支持保存、加载、列出、删除和清理检查点。
每个检查点以 JSON 文件形式存储在 store_dir 目录下。
仅持久化动态变化的状态字段，静态 Agent 配置在恢复时从配置文件重新加载。
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_DYNAMIC_STATE_KEYS: frozenset[str] = frozenset(
    {
        "iteration",
        "ended",
        "user_input",
        "messages",
        "pipeline_id",
        "core_type",
        "conversation_mode",
        "conversation_round",
        # FIND-3 fix: 保存原始 Agent 标识，恢复时按 ID 查找对应 Agent，
        # 避免非灵汐 Agent 的检查点恢复后以灵汐身份运行。
        "agent_config_id",
        # 错误重试计数：suspend/resume 周期必须保留，否则 transient_max_retries
        # 安全阀在恢复后归零，上游持续 timeout 时管道无限重试无法 failed。
        "retry.count",
        "retry.transient_count",
        "error_check.last_error_type",
        "error_check.consecutive_same_type",
    }
)


class PipelineCheckpointManager:
    """管道检查点管理器。

    管理管道执行过程中的状态快照，支持：
    - 保存管道状态快照到 JSON 文件
    - 加载和列出历史检查点
    - 清理过期检查点
    - 按管道 ID 查询最新检查点

    Attributes:
        store_dir: 检查点存储目录路径
    """

    def __init__(self, store_dir: str = "data/pipeline_checkpoints") -> None:
        """初始化检查点管理器。

        Args:
            store_dir: 检查点文件存储目录，默认 data/pipeline_checkpoints
        """
        self.store_dir = Path(store_dir)
        self.store_dir.mkdir(parents=True, exist_ok=True)

    async def save(
        self,
        pipeline_id: str,
        state: dict[str, Any],
        phase: str = "auto",
    ) -> str:
        """保存管道状态快照（仅持久化动态变化的字段）。

        生成唯一的 checkpoint_id，仅将动态状态写入 JSON 文件。
        静态 Agent 配置（system_prompt、constraints、tool_ids 等）不存储，
        恢复时由 PipelineRecovery 从 Agent 配置文件重新加载。

        Args:
            pipeline_id: 管道实例 ID
            state: 管道当前状态字典
            phase: 检查点阶段标记，可选值:
                pre_input / post_input / pre_core / post_core /
                pre_output / post_output / suspended / auto

        Returns:
            生成的 checkpoint_id
        """
        timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S_%f")
        checkpoint_id = f"{pipeline_id}_{timestamp}"

        dynamic_state = {k: v for k, v in state.items() if k in _DYNAMIC_STATE_KEYS}
        serialized_state = self._serialize_state(dynamic_state)

        metadata: dict[str, Any] = {
            "pipeline_id": pipeline_id,
            "checkpoint_id": checkpoint_id,
            "phase": phase,
            "iteration": state.get("iteration", 0),
            "timestamp": datetime.now(UTC).isoformat(),
            "state_keys": list(serialized_state.keys()),
            "version": 2,
        }

        checkpoint_data: dict[str, Any] = {
            "metadata": metadata,
            "state": serialized_state,
        }

        file_path = self.store_dir / f"{checkpoint_id}.json"
        file_path.write_text(
            json.dumps(checkpoint_data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        logger.debug(
            "Checkpoint saved: checkpoint_id=%s, phase=%s, iteration=%d, keys=%s",
            checkpoint_id,
            phase,
            metadata["iteration"],
            list(serialized_state.keys()),
        )
        return checkpoint_id

    async def load(self, checkpoint_id: str) -> dict[str, Any] | None:
        """加载检查点。

        根据 checkpoint_id 读取 JSON 文件并反序列化为状态字典。

        Args:
            checkpoint_id: 检查点 ID

        Returns:
            包含 metadata 和 state 的字典，不存在时返回 None
        """
        file_path = self.store_dir / f"{checkpoint_id}.json"
        if not file_path.exists():
            logger.warning("Checkpoint not found: %s", checkpoint_id)
            return None

        try:
            raw = json.loads(file_path.read_text(encoding="utf-8"))
            raw["state"] = self._deserialize_state(raw.get("state", {}))
            return raw
        except (json.JSONDecodeError, OSError) as exc:
            logger.error("Failed to load checkpoint %s: %s", checkpoint_id, exc)
            return None

    async def list_checkpoints(
        self,
        pipeline_id: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """列出检查点。

        按时间倒序列出检查点元数据，可按 pipeline_id 过滤。

        Args:
            pipeline_id: 管道 ID 过滤条件，None 表示列出全部
            limit: 返回的最大数量，默认 20

        Returns:
            检查点元数据列表，每项包含 metadata 字段
        """
        results: list[dict[str, Any]] = []

        if not self.store_dir.exists():
            return results

        json_files = sorted(
            self.store_dir.glob("*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )

        for file_path in json_files:
            try:
                raw = json.loads(file_path.read_text(encoding="utf-8"))
                metadata = raw.get("metadata", {})

                if pipeline_id and metadata.get("pipeline_id") != pipeline_id:
                    continue

                results.append(metadata)
                if len(results) >= limit:
                    break
            except (json.JSONDecodeError, OSError):
                continue

        return results

    async def delete(self, checkpoint_id: str) -> bool:
        """删除检查点。

        Args:
            checkpoint_id: 要删除的检查点 ID

        Returns:
            删除成功返回 True，文件不存在或删除失败返回 False
        """
        file_path = self.store_dir / f"{checkpoint_id}.json"
        if not file_path.exists():
            logger.warning("Checkpoint not found for deletion: %s", checkpoint_id)
            return False

        try:
            file_path.unlink()
            logger.info("Checkpoint deleted: %s", checkpoint_id)
            return True
        except OSError as exc:
            logger.error("Failed to delete checkpoint %s: %s", checkpoint_id, exc)
            return False

    async def get_latest(self, pipeline_id: str) -> dict[str, Any] | None:
        """获取指定管道的最新检查点。

        Args:
            pipeline_id: 管道 ID

        Returns:
            最新检查点的完整数据（含 metadata 和 state），不存在返回 None
        """
        checkpoints = await self.list_checkpoints(pipeline_id=pipeline_id, limit=1)
        if not checkpoints:
            return None

        latest_id = checkpoints[0].get("checkpoint_id")
        if latest_id is None:
            return None

        return await self.load(latest_id)

    async def get_latest_any(self, phase: str | None = None) -> dict[str, Any] | None:
        """获取所有管道中最新的一条检查点。

        当按 pipeline_id 查找失败时，回退到全局最新检查点，
        确保 CLI 重启后即使 session_id 不匹配也能恢复历史。

        Args:
            phase: 可选的 phase 过滤条件，如 "session_end" / "auto"

        Returns:
            最新检查点的完整数据（含 metadata 和 state），不存在返回 None
        """
        all_checkpoints = await self.list_checkpoints(limit=50)
        if not all_checkpoints:
            return None

        for metadata in all_checkpoints:
            if phase and metadata.get("phase") != phase:
                continue
            latest_id = metadata.get("checkpoint_id")
            if latest_id:
                return await self.load(latest_id)

        return None

    async def cleanup_old(self, pipeline_id: str, keep_count: int = 5) -> int:
        """清理旧检查点，只保留最近的 N 个。

        Args:
            pipeline_id: 管道 ID
            keep_count: 保留的检查点数量，默认 5

        Returns:
            已删除的检查点数量
        """
        all_checkpoints = await self.list_checkpoints(
            pipeline_id=pipeline_id,
            limit=1000,
        )

        if len(all_checkpoints) <= keep_count:
            return 0

        to_delete = all_checkpoints[keep_count:]
        deleted_count = 0

        for metadata in to_delete:
            checkpoint_id = metadata.get("checkpoint_id")
            if checkpoint_id and await self.delete(checkpoint_id):
                deleted_count += 1

        logger.info(
            "Cleaned up %d old checkpoints for pipeline %s (kept %d)",
            deleted_count,
            pipeline_id,
            keep_count,
        )
        return deleted_count

    def _serialize_state(self, state: dict[str, Any]) -> dict[str, Any]:
        """序列化状态字典，处理不可 JSON 序列化的值。

        将不可序列化的值（如函数、异常等）转为字符串表示。

        Args:
            state: 原始状态字典

        Returns:
            可 JSON 序列化的状态字典
        """
        result: dict[str, Any] = {}
        for key, value in state.items():
            try:
                json.dumps(value)
                result[key] = value
            except (TypeError, ValueError, OverflowError):
                result[key] = str(value)
        return result

    def _deserialize_state(self, data: dict[str, Any]) -> dict[str, Any]:
        """反序列化状态字典。

        目前 JSON 反序列化后的数据结构已为基本类型，
        此方法预留扩展点，后续可增加类型还原逻辑。

        Args:
            data: 从 JSON 加载的状态字典

        Returns:
            反序列化后的状态字典
        """
        return data
