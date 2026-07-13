"""任务存储 — 按根任务分目录 + YAML 扁平文件持久化。

存储结构：
- data/tasks/tree_{根任务ID}/{任务ID}.yaml
- 每个任务独立一个 YAML 文件
- 同一根任务树下的所有任务文件放在同一目录
- 内存 dict 缓存 + 文件持久化
- 同步 API（任务系统不涉及高并发写入）
"""

from __future__ import annotations

import contextlib
import logging
from dataclasses import asdict
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

import yaml

from tasks.types import TaskModel, TaskStatus
from utils.enum_utils import safe_enum_value

logger = logging.getLogger(__name__)


# 注册 Enum 的 YAML representer，确保 safe_dump 能正确序列化所有枚举类型
# 修复 metadata 等嵌套结构中残留枚举值导致 RepresenterError 的问题
def _enum_representer(dumper: yaml.Dumper, data: Enum) -> Any:
    return dumper.represent_data(data.value)


yaml.add_multi_representer(Enum, _enum_representer, Dumper=yaml.SafeDumper)


class TaskStorage:
    """任务存储 — 内存缓存 + 按根任务分目录 YAML 持久化。

    每个根任务（parent_task_id 为 None）独占一个 tree_{id}/ 目录，
    目录内每个任务一个 YAML 文件。

    Attributes:
        _tasks: 内存中的任务缓存（task_id → TaskModel）
        _data_dir: 存储根目录路径
    """

    def __init__(self, data_dir: str | Path | None = None) -> None:
        """初始化任务存储。

        Args:
            data_dir: 存储根目录，None 时仅使用内存缓存
        """
        self._tasks: dict[str, TaskModel] = {}
        self._data_dir = Path(data_dir) if data_dir else None
        if self._data_dir:
            self._data_dir.mkdir(parents=True, exist_ok=True)
            self._load_all()

    def _load_all(self) -> None:
        """加载所有任务文件。

        优先加载新的目录结构（tree_*/），再加载旧的顶层 YAML 文件以兼容。
        """
        if not self._data_dir:
            return

        for tree_dir in sorted(self._data_dir.glob("tree_*")):
            if not tree_dir.is_dir():
                continue
            for yaml_file in sorted(tree_dir.glob("*.yaml")):
                self._load_task_file(yaml_file)

    def _load_task_file(self, yaml_file: Path) -> None:
        """加载单个任务 YAML 文件（新格式）。

        新格式：YAML 直接是单个任务的字段字典。

        Args:
            yaml_file: 任务 YAML 文件路径
        """
        try:
            text = yaml_file.read_text(encoding="utf-8")
            data = yaml.safe_load(text)
            if not isinstance(data, dict):
                return
            task = self._dict_to_task(data)
            self._tasks[task.id] = task
        except Exception as exc:
            logger.warning("加载任务文件失败: %s — %s", yaml_file, exc)

    def _find_root_id(self, task: TaskModel) -> str:
        """查找任务所属的根任务ID。

        沿 parent_task_id 链递归回溯，直到找到 parent_task_id 为 None 的根任务。
        支持多层嵌套（如 A → B → C，C 的根是 A）。

        Args:
            task: 任务模型

        Returns:
            根任务ID
        """
        visited: set[str] = set()
        current_id: str | None = task.id
        current_parent: str | None = task.parent_task_id

        while current_parent:
            if current_parent in visited:
                logger.warning(
                    "检测到 parent_task_id 循环: %s, 截断使用 %s",
                    visited,
                    current_parent,
                )
                break
            visited.add(current_parent)
            parent_task = self._tasks.get(current_parent)
            if parent_task is None:
                break
            current_id = parent_task.id
            current_parent = parent_task.parent_task_id

        return current_id if current_id else task.id

    def _get_tree_dir(self, root_id: str) -> Path | None:
        """获取根任务对应的目录路径。

        Args:
            root_id: 根任务ID

        Returns:
            目录路径，无 data_dir 时返回 None
        """
        if not self._data_dir:
            return None
        return self._data_dir / f"tree_{root_id}"

    def _get_task_file_path(self, root_id: str, task_id: str) -> Path | None:
        """获取任务文件的完整路径。

        Args:
            root_id: 根任务ID
            task_id: 任务ID

        Returns:
            任务 YAML 文件路径，无 data_dir 时返回 None
        """
        tree_dir = self._get_tree_dir(root_id)
        if tree_dir is None:
            return None
        return tree_dir / f"{task_id}.yaml"

    def _ensure_tree_dir(self, root_id: str) -> Path | None:
        """确保根任务目录存在。

        Args:
            root_id: 根任务ID

        Returns:
            目录路径，无 data_dir 时返回 None
        """
        tree_dir = self._get_tree_dir(root_id)
        if tree_dir is None:
            return None
        tree_dir.mkdir(parents=True, exist_ok=True)
        return tree_dir

    def _persist_task(self, task: TaskModel) -> None:
        """将单个任务持久化到对应的 YAML 文件。

        Args:
            task: 要持久化的任务模型
        """
        if not self._data_dir:
            return
        root_id = self._find_root_id(task)
        self._ensure_tree_dir(root_id)
        file_path = self._get_task_file_path(root_id, task.id)
        if file_path is None:
            return
        data = self._task_to_dict(task)
        file_path.write_text(
            yaml.safe_dump(data, default_flow_style=False, allow_unicode=True, sort_keys=False, indent=2),
            encoding="utf-8",
        )

    @staticmethod
    def _task_to_dict(task: TaskModel) -> dict[str, Any]:
        """将 TaskModel 转换为可序列化的字典。

        枚举字段转换为原始值，便于 YAML 序列化。

        Args:
            task: 任务模型

        Returns:
            可序列化的任务字典
        """
        d = asdict(task)
        d["status"] = safe_enum_value(task.status)
        d["priority"] = safe_enum_value(task.priority)
        d["agent_level"] = safe_enum_value(task.agent_level)
        return d

    @staticmethod
    def _dict_to_task(data: dict[str, Any]) -> TaskModel:
        """将字典反序列化为 TaskModel。

        字符串状态/优先级/层级字段转换回枚举值。

        Args:
            data: 任务字典

        Returns:
            TaskModel 实例
        """
        from agents.types import AgentLevel  # noqa: PLC0415
        from tasks.types import TaskPriority  # noqa: PLC0415

        # 兼容历史脏数据：description 曾被 LLM 写成 list，反序列化时归一化为 str，
        # 否则 API 层 TaskResponse.description（pydantic 强制 str）校验失败。
        raw_desc = data.get("description", "")
        if not isinstance(raw_desc, str):
            data["description"] = (
                "\n".join(str(item) for item in raw_desc) if isinstance(raw_desc, (list, tuple)) else str(raw_desc)
            )

        if isinstance(data.get("status"), str):
            data["status"] = TaskStatus(data["status"])
        if isinstance(data.get("priority"), int) and not isinstance(data["priority"], TaskPriority):
            data["priority"] = TaskPriority(data["priority"])
        if isinstance(data.get("agent_level"), str) and not isinstance(data["agent_level"], AgentLevel):
            data["agent_level"] = AgentLevel(data["agent_level"])
        return TaskModel(**data)

    def save(self, task: TaskModel) -> None:
        """保存任务到内存缓存并持久化到文件。

        Args:
            task: 要保存的任务模型
        """
        self._tasks[task.id] = task
        self._persist_task(task)

    def get(self, task_id: str) -> TaskModel | None:
        """从内存缓存获取任务。

        Args:
            task_id: 任务ID

        Returns:
            任务模型，不存在时返回 None
        """
        return self._tasks.get(task_id)

    def update(self, task_id: str, **updates: Any) -> TaskModel | None:
        """更新任务字段并持久化。

        Args:
            task_id: 任务ID
            **updates: 要更新的字段键值对

        Returns:
            更新后的任务模型，不存在时返回 None
        """
        task = self._tasks.get(task_id)
        if task is None:
            return None
        for key, value in updates.items():
            if hasattr(task, key):
                setattr(task, key, value)
        task.updated_at = datetime.now().isoformat()
        self._persist_task(task)
        return task

    def list_by_status(self, status: TaskStatus) -> list[TaskModel]:
        """按状态筛选任务。

        Args:
            status: 任务状态

        Returns:
            匹配状态的任务列表
        """
        return [t for t in self._tasks.values() if t.status == status]

    def list_by_parent(self, parent_id: str) -> list[TaskModel]:
        """列出指定父任务的所有直接子任务。

        Args:
            parent_id: 父任务ID

        Returns:
            子任务列表
        """
        return [t for t in self._tasks.values() if t.parent_task_id == parent_id]

    def delete(self, task_id: str) -> bool:
        """删除任务。

        从内存缓存和文件系统中移除任务。如果删除的是根任务
        且该目录下已无其他任务文件，则删除整个目录。

        Args:
            task_id: 要删除的任务ID

        Returns:
            是否删除成功
        """
        if task_id not in self._tasks:
            return False
        task = self._tasks[task_id]
        root_id = self._find_root_id(task)
        del self._tasks[task_id]

        file_path = self._get_task_file_path(root_id, task_id)
        if file_path and file_path.exists():
            file_path.unlink()

        if root_id == task_id:
            remaining = any(t.parent_task_id == root_id for t in self._tasks.values())
            if not remaining:
                tree_dir = self._get_tree_dir(root_id)
                if tree_dir and tree_dir.exists():
                    with contextlib.suppress(OSError):
                        tree_dir.rmdir()

        return True
