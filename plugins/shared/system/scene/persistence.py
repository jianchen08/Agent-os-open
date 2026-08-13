"""场景状态持久化模块。

使用 JSON 文件存储场景数据，支持场景的 CRUD 操作和状态持久化。

暴露接口：
- ScenePersistence: 场景持久化管理类
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

from scene.models import Scene

# 多租户数据根咽喉点（plugins/shared/tenant_data.py）。本文件位于
# plugins/shared/system/scene/persistence.py，上溯 2 级到 plugins/shared/。
# 参考 hindsight_memory/wiring.py 的 sys.path 自举模式。
_SHARED_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _SHARED_ROOT not in sys.path:
    sys.path.insert(0, _SHARED_ROOT)
from tenant_data import DEFAULT_TENANT, tenant_data_root  # noqa: E402

logger = logging.getLogger(__name__)


class ScenePersistence:
    """场景 JSON 文件持久化管理。

    将场景数据保存到 JSON 文件中，支持读写操作。

    Attributes:
        storage_path: 存储目录路径
        scenes_file: 场景数据文件路径
    """

    def __init__(
        self,
        storage_path: str | Path | None = None,
        tenant_id: str | None = None,
    ) -> None:
        """初始化持久化管理器。

        storage_path 解析优先级（高 → 低）：

        1. 显式 ``storage_path`` 参数（测试/定制路径，最高优先级，覆盖一切）；
        2. 环境变量 ``SCENES_STORAGE_DIR``（兼容存量部署覆盖）；
        3. 多租户数据根 ``tenant_data_root(tenant_id or default, "scenes")``
           （方案 B 目录隔离默认值，即 ``data/{tenant_id}/scenes``）。

        Args:
            storage_path: 存储目录路径。显式传入时优先使用，覆盖 env 与
                tenant_id。
            tenant_id: 租户 ID。未传 storage_path 且未设 env 时，存储目录落在
                ``data/{tenant_id}/scenes``；None 则用 ``DEFAULT_TENANT``。
        """
        if storage_path is not None:
            resolved = storage_path
        else:
            env_dir = os.environ.get("SCENES_STORAGE_DIR")
            if env_dir:
                resolved = env_dir
            else:
                # 方案 B 默认：经多租户咽喉点取 data/{tenant_id}/scenes
                resolved = tenant_data_root(tenant_id or DEFAULT_TENANT, "scenes")
        self.storage_path = Path(resolved)
        self.scenes_file = self.storage_path / "scenes.json"
        self._ensure_storage_dir()

    def _ensure_storage_dir(self) -> None:
        """确保存储目录存在。"""
        self.storage_path.mkdir(parents=True, exist_ok=True)

    def _read_all_raw(self) -> dict[str, Any]:
        """读取所有场景原始数据。

        Returns:
            包含所有场景数据的字典，格式为 {"scenes": {scene_id: scene_dict}}
        """
        if not self.scenes_file.exists():
            return {"scenes": {}}

        try:
            content = self.scenes_file.read_text(encoding="utf-8")
            if not content.strip():
                return {"scenes": {}}
            return json.loads(content)
        except (json.JSONDecodeError, OSError) as exc:
            logger.error("读取场景数据失败: %s", exc)
            return {"scenes": {}}

    def _write_all_raw(self, data: dict[str, Any]) -> None:
        """写入所有场景原始数据。

        Args:
            data: 包含所有场景数据的字典
        """
        self._ensure_storage_dir()
        try:
            content = json.dumps(data, ensure_ascii=False, indent=2)
            self.scenes_file.write_text(content, encoding="utf-8")
        except OSError as exc:
            logger.error("写入场景数据失败: %s", exc)
            raise

    def load_scenes(self) -> list[Scene]:
        """加载所有场景。

        Returns:
            场景列表
        """
        raw = self._read_all_raw()
        scenes_data = raw.get("scenes", {})
        scenes: list[Scene] = []
        for scene_dict in scenes_data.values():
            try:
                scenes.append(Scene.model_validate(scene_dict))
            except Exception as exc:
                logger.warning("跳过无效场景数据: %s", exc)
        return scenes

    def save_scene(self, scene: Scene) -> None:
        """保存单个场景（新增或更新）。

        Args:
            scene: 要保存的场景对象
        """
        raw = self._read_all_raw()
        raw.setdefault("scenes", {})[scene.id] = scene.model_dump(mode="json")
        self._write_all_raw(raw)

    def delete_scene(self, scene_id: str) -> bool:
        """删除指定场景。

        Args:
            scene_id: 要删除的场景 ID

        Returns:
            是否成功删除
        """
        raw = self._read_all_raw()
        scenes = raw.get("scenes", {})
        if scene_id not in scenes:
            return False
        del scenes[scene_id]
        self._write_all_raw(raw)
        return True

    def get_scene(self, scene_id: str) -> Scene | None:
        """获取指定场景。

        Args:
            scene_id: 场景 ID

        Returns:
            场景对象，不存在则返回 None
        """
        raw = self._read_all_raw()
        scene_dict = raw.get("scenes", {}).get(scene_id)
        if scene_dict is None:
            return None
        try:
            return Scene.model_validate(scene_dict)
        except Exception as exc:
            logger.warning("场景数据无效: %s", exc)
            return None

    def save_all_scenes(self, scenes: list[Scene]) -> None:
        """批量保存所有场景。

        Args:
            scenes: 场景列表
        """
        raw: dict[str, Any] = {"scenes": {s.id: s.model_dump(mode="json") for s in scenes}}
        self._write_all_raw(raw)
