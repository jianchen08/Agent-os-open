"""任务存储 — YAML 多文件持久化。

按根任务拆分为独立 YAML 文件：
- 目录结构：data/tasks/{root_task_id}.yaml
- 每个 YAML 文件包含一个根任务及其所有子任务
- 内存 dict 缓存 + 文件持久化
- 同步 API（任务系统不涉及高并发写入）
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from tasks.types import TaskModel, TaskStatus


class TaskStorage:
    """任务存储 — 内存缓存 + YAML 多文件持久化。

    按根任务（parent_task_id 为 None 的任务）拆分为独立 YAML 文件。
    内存缓存仍用 dict[str, TaskModel]，方便查询。

    Attributes:
        _tasks: 内存中的任务缓存
        _data_dir: YAML 文件目录路径
    """

    def __init__(self, data_dir: str | Path | None = None) -> None:
        self._tasks: dict[str, TaskModel] = {}
        self._data_dir = Path(data_dir) if data_dir else None
        if self._data_dir:
            self._data_dir.mkdir(parents=True, exist_ok=True)
            self._load_all()

    def _load_all(self) -> None:
        if not self._data_dir:
            return
        for yaml_file in self._data_dir.glob("*.yaml"):
            self._load_file(yaml_file)

    def _load_file(self, yaml_file: Path) -> None:
        try:
            text = yaml_file.read_text(encoding="utf-8")
            data = yaml.safe_load(text)
            if not isinstance(data, dict):
                return
            tasks_list = data.get("tasks")
            if not tasks_list or not isinstance(tasks_list, list):
                return
            for task_dict in tasks_list:
                if isinstance(task_dict, dict):
                    task = self._dict_to_task(task_dict)
                    self._tasks[task.id] = task
        except Exception:
            pass

    def _find_root_id(self, task: TaskModel) -> str:
        if task.parent_task_id:
            return task.parent_task_id
        return task.id

    def _get_file_path(self, root_id: str) -> Path | None:
        if not self._data_dir:
            return None
        return self._data_dir / f"{root_id}.yaml"

    def _persist_root(self, root_id: str) -> None:
        if not self._data_dir:
            return
        file_path = self._get_file_path(root_id)
        if file_path is None:
            return
        self._data_dir.mkdir(parents=True, exist_ok=True)
        root_task = self._tasks.get(root_id)
        subtasks = [t for t in self._tasks.values() if t.parent_task_id == root_id]
        tasks_in_file: list[dict[str, Any]] = []
        if root_task:
            tasks_in_file.append(self._task_to_dict(root_task))
        for sub in subtasks:
            tasks_in_file.append(self._task_to_dict(sub))
        data = {"tasks": tasks_in_file}
        file_path.write_text(
            yaml.safe_dump(data, default_flow_style=False, allow_unicode=True, sort_keys=False, indent=2),
            encoding="utf-8",
        )

    @staticmethod
    def _task_to_dict(task: TaskModel) -> dict[str, Any]:
        d = asdict(task)
        d["status"] = task.status.value if hasattr(task.status, "value") else task.status
        d["priority"] = task.priority.value if hasattr(task.priority, "value") else task.priority
        d["agent_level"] = task.agent_level.value if hasattr(task.agent_level, "value") else task.agent_level
        return d

    @staticmethod
    def _dict_to_task(data: dict[str, Any]) -> TaskModel:
        from pipeline.types import AgentLevel, TaskPriority

        if isinstance(data.get("status"), str):
            data["status"] = TaskStatus(data["status"])
        if isinstance(data.get("priority"), int) and not isinstance(data["priority"], TaskPriority):
            data["priority"] = TaskPriority(data["priority"])
        if isinstance(data.get("agent_level"), str) and not isinstance(data["agent_level"], AgentLevel):
            data["agent_level"] = AgentLevel(data["agent_level"])
        return TaskModel(**data)

    def save(self, task: TaskModel) -> None:
        self._tasks[task.id] = task
        root_id = self._find_root_id(task)
        self._persist_root(root_id)

    def get(self, task_id: str) -> TaskModel | None:
        return self._tasks.get(task_id)

    def update(self, task_id: str, **updates: Any) -> TaskModel | None:
        task = self._tasks.get(task_id)
        if task is None:
            return None
        for key, value in updates.items():
            if hasattr(task, key):
                setattr(task, key, value)
        task.updated_at = datetime.now().isoformat()
        root_id = self._find_root_id(task)
        self._persist_root(root_id)
        return task

    def list_by_status(self, status: TaskStatus) -> list[TaskModel]:
        return [t for t in self._tasks.values() if t.status == status]

    def list_by_parent(self, parent_id: str) -> list[TaskModel]:
        return [t for t in self._tasks.values() if t.parent_task_id == parent_id]

    def delete(self, task_id: str) -> bool:
        if task_id not in self._tasks:
            return False
        task = self._tasks[task_id]
        root_id = self._find_root_id(task)
        del self._tasks[task_id]
        if root_id == task_id:
            subtask_ids = [t.id for t in self._tasks.values() if t.parent_task_id == root_id]
            if not subtask_ids:
                file_path = self._get_file_path(root_id)
                if file_path and file_path.exists():
                    file_path.unlink()
                return True
        self._persist_root(root_id)
        return True
