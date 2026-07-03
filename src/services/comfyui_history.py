"""
ComfyUI 生成历史记录模型与持久化

管理图像生成任务的记录，支持 JSON 文件存储、增删查改、按时间排序和分页。

暴露接口：
- GenerationStatus: 生成状态枚举
- GenerationRecord: 生成记录数据类
- GenerationHistory: 生成历史管理类
"""

from __future__ import annotations

import json
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class GenerationStatus:
    """生成任务状态常量。"""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class GenerationRecord:
    """单次图像生成的完整记录。

    Attributes:
        id: 记录唯一标识
        prompt: 正向提示词
        negative_prompt: 负向提示词
        template_name: 使用的工作流模板名称
        parameters: 生成参数（width, height, steps, cfg_scale, seed 等）
        status: 当前状态（pending/running/completed/failed）
        progress: 进度百分比（0-100）
        result_images: 生成结果图片 URL 列表
        created_at: 创建时间（ISO 格式）
        completed_at: 完成时间（ISO 格式），未完成为 None
        error: 错误信息，无错误时为 None
    """

    id: str = ""
    prompt: str = ""
    negative_prompt: str = ""
    template_name: str = ""
    parameters: dict[str, Any] = field(default_factory=dict)
    status: str = GenerationStatus.PENDING
    progress: int = 0
    result_images: list[str] = field(default_factory=list)
    created_at: str = ""
    completed_at: str | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        """自动填充 ID 和创建时间。"""
        if not self.id:
            self.id = uuid.uuid4().hex[:16]
        if not self.created_at:
            self.created_at = _now_iso()

    def to_dict(self) -> dict[str, Any]:
        """转换为字典，用于 JSON 序列化。

        Returns:
            记录的字典表示
        """
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GenerationRecord:
        """从字典创建记录实例。

        Args:
            data: 字典数据

        Returns:
            GenerationRecord 实例
        """
        return cls(**data)


def _now_iso() -> str:
    """返回当前 UTC 时间的 ISO 格式字符串。"""
    return datetime.now(timezone.utc).isoformat()


class GenerationHistory:
    """生成历史管理，JSON 文件持久化存储。

    支持增删查改、按时间倒序排列、分页查询。

    Attributes:
        records: 所有生成记录，key 为记录 ID
    """

    def __init__(self, file_path: str | Path = "data/comfyui_history.json") -> None:
        """初始化生成历史管理器。

        Args:
            file_path: JSON 存储文件路径
        """
        self._file_path = Path(file_path)
        self._records: dict[str, GenerationRecord] = {}
        self._lock = threading.Lock()
        self._load()

    def _load(self) -> None:
        """从 JSON 文件加载历史记录。"""
        if not self._file_path.exists():
            return
        try:
            content = self._file_path.read_text(encoding="utf-8")
            data = json.loads(content)
            if isinstance(data, dict):
                for record_id, record_data in data.get("records", {}).items():
                    self._records[record_id] = GenerationRecord.from_dict(record_data)
        except (json.JSONDecodeError, KeyError, TypeError):
            pass

    def _save(self) -> None:
        """将历史记录持久化到 JSON 文件。"""
        try:
            self._file_path.parent.mkdir(parents=True, exist_ok=True)
            data = {"records": {rid: record.to_dict() for rid, record in self._records.items()}}
            tmp_path = self._file_path.with_suffix(".tmp")
            tmp_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp_path.replace(self._file_path)
        except Exception:
            pass

    def add(self, record: GenerationRecord) -> GenerationRecord:
        """添加一条生成记录。

        Args:
            record: 要添加的记录

        Returns:
            添加后的记录（含自动生成的 ID）
        """
        with self._lock:
            self._records[record.id] = record
            self._save()
        return record

    def get(self, record_id: str) -> GenerationRecord | None:
        """根据 ID 获取单条记录。

        Args:
            record_id: 记录 ID

        Returns:
            记录实例，不存在时返回 None
        """
        return self._records.get(record_id)

    def update(self, record_id: str, **kwargs: Any) -> GenerationRecord | None:
        """更新指定记录的字段。

        Args:
            record_id: 记录 ID
            **kwargs: 要更新的字段键值对

        Returns:
            更新后的记录，不存在时返回 None
        """
        with self._lock:
            record = self._records.get(record_id)
            if record is None:
                return None
            for key, value in kwargs.items():
                if hasattr(record, key):
                    setattr(record, key, value)
            self._save()
        return record

    def delete(self, record_id: str) -> bool:
        """删除指定记录。

        Args:
            record_id: 记录 ID

        Returns:
            是否删除成功
        """
        with self._lock:
            if record_id not in self._records:
                return False
            del self._records[record_id]
            self._save()
        return True

    def list_records(
        self,
        limit: int = 20,
        offset: int = 0,
        status: str | None = None,
    ) -> tuple[list[GenerationRecord], int]:
        """查询记录列表，按创建时间倒序排列。

        Args:
            limit: 每页数量
            offset: 偏移量
            status: 可选的状态过滤

        Returns:
            (记录列表, 总数)
        """
        records = list(self._records.values())
        if status is not None:
            records = [r for r in records if r.status == status]
        records.sort(key=lambda r: r.created_at, reverse=True)
        total = len(records)
        page = records[offset : offset + limit]
        return page, total

    def count(self, status: str | None = None) -> int:
        """统计记录数量。

        Args:
            status: 可选的状态过滤

        Returns:
            记录数量
        """
        if status is None:
            return len(self._records)
        return sum(1 for r in self._records.values() if r.status == status)
