"""场景管理器。

提供场景的创建、切换、删除、查询等核心操作，自动处理状态持久化。

暴露接口：
- SceneManager: 场景管理器类
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from .models import Scene, SceneLayoutConfig, SceneUpdateRequest
from .persistence import ScenePersistence
from .templates import get_template

logger = logging.getLogger(__name__)


class SceneManager:
    """场景管理器。

    管理场景的完整生命周期：创建、查询、切换、更新、删除。
    自动处理场景切换时的状态保存和恢复。

    Attributes:
        _persistence: 持久化存储实例
        _active_scene_id: 当前活跃场景 ID
    """

    def __init__(self, persistence: ScenePersistence | None = None) -> None:
        """初始化场景管理器。

        Args:
            persistence: 持久化存储实例，默认使用 JSON 文件存储
        """
        self._persistence = persistence or ScenePersistence()
        self._active_scene_id: str | None = None
        self._load_active_state()

    def _load_active_state(self) -> None:
        """从持久化数据中恢复活跃场景状态。"""
        scenes = self._persistence.load_scenes()
        for scene in scenes:
            if scene.is_active:
                self._active_scene_id = scene.id
                break

    def create_scene(
        self,
        name: str,
        description: str = "",
        template_id: str | None = None,
        layout: SceneLayoutConfig | None = None,
        widgets: list[dict[str, Any]] | None = None,
    ) -> Scene:
        """创建新场景。

        如果指定了 template_id，则基于模板创建场景，未覆盖的字段使用模板默认值。

        Args:
            name: 场景名称
            description: 场景描述
            template_id: 模板 ID（可选）
            layout: 布局配置（可选，覆盖模板）
            widgets: 组件列表（可选，覆盖模板）

        Returns:
            创建的场景对象

        Raises:
            ValueError: 指定的模板 ID 不存在
        """
        # 基于模板创建
        template = None
        if template_id:
            template = get_template(template_id)
            if template is None:
                raise ValueError(f"模板不存在: {template_id}")

        # 合并模板与传入参数（传入参数优先）
        scene_layout = layout or (template.layout if template else SceneLayoutConfig())

        scene_widgets = []
        if widgets:
            from .models import SceneWidgetConfig  # noqa: PLC0415

            scene_widgets = [SceneWidgetConfig(**w) for w in widgets]
        elif template:
            scene_widgets = list(template.widgets)

        scene = Scene(
            name=name,
            description=description or (template.description if template else ""),
            template_id=template_id,
            layout=scene_layout,
            widgets=scene_widgets,
            is_active=False,
        )

        self._persistence.save_scene(scene)
        logger.info("创建场景: %s (%s)", scene.name, scene.id)
        return scene

    def switch_scene(self, scene_id: str) -> Scene:
        """切换活跃场景。

        自动保存前一场景的状态，并将目标场景设为活跃。

        Args:
            scene_id: 目标场景 ID

        Returns:
            切换后的活跃场景

        Raises:
            ValueError: 指定的场景不存在
        """
        # 验证目标场景存在
        target = self._persistence.get_scene(scene_id)
        if target is None:
            raise ValueError(f"场景不存在: {scene_id}")

        # 保存前一场景状态（取消活跃标记）
        if self._active_scene_id and self._active_scene_id != scene_id:
            prev_scene = self._persistence.get_scene(self._active_scene_id)
            if prev_scene:
                prev_scene.is_active = False
                prev_scene.updated_at = datetime.now().isoformat()
                self._persistence.save_scene(prev_scene)
                logger.debug("自动保存场景状态: %s", prev_scene.name)

        # 激活目标场景
        target.is_active = True
        target.updated_at = datetime.now().isoformat()
        self._persistence.save_scene(target)
        self._active_scene_id = scene_id

        logger.info("切换场景: %s (%s)", target.name, target.id)
        return target

    def delete_scene(self, scene_id: str) -> bool:
        """删除场景及其关联数据。

        如果删除的是当前活跃场景，则清除活跃状态。

        Args:
            scene_id: 要删除的场景 ID

        Returns:
            是否成功删除
        """
        scene = self._persistence.get_scene(scene_id)
        if scene is None:
            return False

        # 如果删除的是活跃场景，清除活跃状态
        if self._active_scene_id == scene_id:
            self._active_scene_id = None

        result = self._persistence.delete_scene(scene_id)
        if result:
            logger.info("删除场景: %s (%s)", scene.name, scene_id)
        return result

    def list_scenes(self) -> list[Scene]:
        """列出所有场景。

        Returns:
            场景列表
        """
        return self._persistence.load_scenes()

    def get_scene(self, scene_id: str) -> Scene | None:
        """获取场景详情。

        Args:
            scene_id: 场景 ID

        Returns:
            场景对象，不存在则返回 None
        """
        return self._persistence.get_scene(scene_id)

    def update_scene(self, scene_id: str, request: SceneUpdateRequest) -> Scene | None:
        """更新场景。

        Args:
            scene_id: 场景 ID
            request: 更新请求

        Returns:
            更新后的场景，不存在则返回 None
        """
        scene = self._persistence.get_scene(scene_id)
        if scene is None:
            return None

        # 仅更新提供的字段
        if request.name is not None:
            scene.name = request.name
        if request.description is not None:
            scene.description = request.description
        if request.layout is not None:
            scene.layout = request.layout
        if request.widgets is not None:
            scene.widgets = request.widgets
        if request.state is not None:
            scene.state = request.state

        scene.updated_at = datetime.now().isoformat()
        self._persistence.save_scene(scene)
        logger.info("更新场景: %s (%s)", scene.name, scene.id)
        return scene

    def get_active_scene(self) -> Scene | None:
        """获取当前活跃场景。

        Returns:
            活跃场景，无则返回 None
        """
        if self._active_scene_id is None:
            return None
        return self._persistence.get_scene(self._active_scene_id)
