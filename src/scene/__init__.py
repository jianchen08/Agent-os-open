"""场景管理模块。

提供场景管理系统的核心功能，包括：
- models: 场景数据模型（Scene、SceneState、SceneTemplate 等）
- manager: SceneManager 场景管理器，实现 CRUD、切换、状态持久化
- templates: 预设场景模板库（≥3 个模板）
- persistence: 场景状态持久化（JSON 文件存储）

主要组件：
    - Scene: 场景数据模型
    - SceneState: 场景状态快照
    - SceneTemplate: 场景模板
    - SceneManager: 场景管理器
    - PRESET_TEMPLATES: 预设模板列表

使用示例：
    >>> from scene import SceneManager
    >>> manager = SceneManager()
    >>>
    >>> # 创建场景
    >>> scene = manager.create_scene("工作台", description="我的工作台")
    >>>
    >>> # 基于模板创建
    >>> scene = manager.create_scene("聊天", template_id="chat_workspace")
    >>>
    >>> # 切换场景
    >>> manager.switch_scene(scene.id)
    >>>
    >>> # 列出场景
    >>> scenes = manager.list_scenes()
"""

from .manager import SceneManager
from .models import (
    Scene,
    SceneCreateRequest,
    SceneLayoutConfig,
    SceneLayoutType,
    SceneState,
    SceneTemplate,
    SceneUpdateRequest,
    SceneWidgetConfig,
)
from .persistence import ScenePersistence
from .templates import PRESET_TEMPLATES, get_template, list_templates

__all__ = [
    # 管理器
    "SceneManager",
    # 持久化
    "ScenePersistence",
    # 模型
    "Scene",
    "SceneState",
    "SceneTemplate",
    "SceneCreateRequest",
    "SceneUpdateRequest",
    "SceneLayoutConfig",
    "SceneLayoutType",
    "SceneWidgetConfig",
    # 模板
    "PRESET_TEMPLATES",
    "get_template",
    "list_templates",
]

__version__ = "1.0.0"
