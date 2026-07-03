"""Schema 解析器。

从模块 YAML 配置文件中解析 ``ui:`` 部分为 ``ModuleUISchema`` 对象，
同时解析顶层 ``data:`` 声明用于 CRUD 自动生成。
支持目录批量加载、单文件加载和热重载变更检测。
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
from pathlib import Path
from typing import Any

import yaml

from ui_schema.types import (
    ChatInteractionConfig,
    ClientCapabilities,
    ModuleAction,
    ModuleIdentity,
    ModuleRendering,
    ModuleUISchema,
    RenderingSpaceConfig,
)

logger = logging.getLogger(__name__)


class SchemaParser:
    """UI Schema 解析器。

    从 YAML 配置文件中提取 ``ui`` 部分并解析为 ``ModuleUISchema``。

    Attributes:
        _schemas: 已解析的 Schema 缓存，key 为 module id。
        _file_mtimes: 文件修改时间缓存，用于热重载检测。
    """

    def __init__(self) -> None:
        """初始化解析器。"""
        self._schemas: dict[str, ModuleUISchema] = {}
        self._file_mtimes: dict[str, float] = {}
        self._file_hashes: dict[str, str] = {}
        # data 段缓存：key 为 module_id，value 为 data 声明字典
        self._data_decls: dict[str, dict[str, Any]] = {}

    def load_directory(self, dir_path: str | Path) -> list[ModuleUISchema]:
        """从目录批量加载 YAML 配置文件。

        遍历目录下所有 ``.yaml`` / ``.yml`` 文件，提取包含 ``ui`` 部分的配置。

        Args:
            dir_path: 配置目录路径。

        Returns:
            解析成功的 ModuleUISchema 列表。
        """
        dir_path = Path(dir_path)
        if not dir_path.exists() or not dir_path.is_dir():
            logger.warning("Schema 目录不存在或不是目录: %s", dir_path)
            return []

        schemas: list[ModuleUISchema] = []
        for yaml_file in sorted(dir_path.rglob("*.yaml")):
            schema = self.load_file(yaml_file)
            if schema is not None:
                self._schemas[schema.identity.id] = schema
                schemas.append(schema)

        for yaml_file in sorted(dir_path.rglob("*.yml")):
            schema = self.load_file(yaml_file)
            if schema is not None and schema.identity.id not in self._schemas:
                self._schemas[schema.identity.id] = schema
                schemas.append(schema)

        logger.info("从 %s 加载了 %d 个 UI Schema", dir_path, len(schemas))
        return schemas

    def load_file(self, file_path: str | Path) -> ModuleUISchema | None:  # noqa: PLR0911
        """加载单个 YAML 配置文件。

        Args:
            file_path: YAML 文件路径。

        Returns:
            解析成功的 ModuleUISchema，无 ``ui`` 部分或解析失败返回 None。
        """
        file_path = Path(file_path)
        if not file_path.exists():
            logger.debug("文件不存在: %s", file_path)
            return None

        try:
            with open(file_path, encoding="utf-8") as f:
                raw: dict[str, Any] = yaml.safe_load(f) or {}
        except yaml.YAMLError as exc:
            logger.warning("YAML 解析失败: %s | %s", file_path, exc)
            return None
        except OSError as exc:
            logger.warning("文件读取失败: %s | %s", file_path, exc)
            return None

        if not isinstance(raw, dict):
            logger.debug("YAML 内容非字典，跳过: %s", file_path)
            return None

        # 记录文件修改时间和哈希
        self._record_file_meta(file_path, raw)

        # 提取顶层 data 段（用于 CRUD 自动生成）
        data_section = raw.get("data")
        if isinstance(data_section, dict):
            self._extract_and_cache_data(raw, data_section)

        ui_data = raw.get("ui")
        if ui_data is None:
            logger.debug("文件无 ui 部分，跳过: %s", file_path)
            return None

        if not isinstance(ui_data, dict):
            logger.warning("ui 部分非字典: %s", file_path)
            return None

        return self._parse_ui_section(ui_data, file_path)

    def get_schema(self, module_id: str) -> ModuleUISchema | None:
        """获取已缓存的指定模块 Schema。

        Args:
            module_id: 模块 ID。

        Returns:
            ModuleUISchema 或 None。
        """
        return self._schemas.get(module_id)

    def list_schemas(self) -> list[ModuleUISchema]:
        """获取所有已缓存的 Schema。

        Returns:
            ModuleUISchema 列表。
        """
        return list(self._schemas.values())

    def get_data_decls(self, module_id: str) -> dict[str, Any] | None:
        """获取指定模块的 data 声明。

        Args:
            module_id: 模块 ID。

        Returns:
            data 声明字典（key 为集合名，value 为集合定义），不存在返回 None。
        """
        return self._data_decls.get(module_id)

    def list_all_data_decls(self) -> dict[str, dict[str, Any]]:
        """获取所有模块的 data 声明。

        Returns:
            字典，key 为 module_id，value 为该模块的 data 声明。
        """
        return dict(self._data_decls)

    def detect_changes(self, dir_path: str | Path) -> dict[str, str]:
        """检测目录中文件的变更。

        通过文件修改时间和内容哈希检测变更。

        Args:
            dir_path: 配置目录路径。

        Returns:
            变更字典，key 为 module id，value 为变更类型（changed/added/removed）。
        """
        dir_path = Path(dir_path)
        if not dir_path.exists():
            return {}

        changed: dict[str, str] = {}

        # 检测现有文件的变更
        current_files: set[str] = set()
        for yaml_file in dir_path.rglob("*.yaml"):
            current_files.add(str(yaml_file))
            self._check_file_change(yaml_file, changed)
        for yaml_file in dir_path.rglob("*.yml"):
            key = str(yaml_file)
            if key not in current_files:
                current_files.add(key)
                self._check_file_change(yaml_file, changed)

        return changed

    def _record_file_meta(self, file_path: Path, raw: dict[str, Any]) -> None:
        """记录文件元数据用于变更检测。

        Args:
            file_path: 文件路径。
            raw: 解析后的 YAML 数据。
        """
        key = str(file_path)
        with contextlib.suppress(OSError):
            self._file_mtimes[key] = file_path.stat().st_mtime

        content_str = str(raw.get("ui", ""))
        self._file_hashes[key] = hashlib.md5(content_str.encode(), usedforsecurity=False).hexdigest()

    def _check_file_change(self, file_path: Path, changed: dict[str, str]) -> None:
        """检查单个文件是否有变更。

        Args:
            file_path: 文件路径。
            changed: 变更字典，会就地修改。
        """
        key = str(file_path)
        if not file_path.exists():
            return

        try:
            current_mtime = file_path.stat().st_mtime
        except OSError:
            return

        old_mtime = self._file_mtimes.get(key)
        if old_mtime is None:
            # 新文件
            changed[key] = "added"
            return

        if current_mtime <= old_mtime:
            return

        # mtime 变了，进一步检查内容哈希
        try:
            with open(file_path, encoding="utf-8") as f:
                raw: dict[str, Any] = yaml.safe_load(f) or {}
        except (yaml.YAMLError, OSError):
            return

        ui_str = str(raw.get("ui", ""))
        current_hash = hashlib.md5(ui_str.encode(), usedforsecurity=False).hexdigest()
        old_hash = self._file_hashes.get(key, "")

        if current_hash != old_hash:
            # 提取 module id 作为 key
            ui_data = raw.get("ui", {})
            if isinstance(ui_data, dict):
                identity = ui_data.get("identity", {})
                module_id = identity.get("id", "") if isinstance(identity, dict) else ""
                if module_id:
                    changed[module_id] = "changed"

    def _parse_ui_section(self, ui_data: dict[str, Any], file_path: Path) -> ModuleUISchema | None:
        """解析 YAML 中的 ui 部分。

        自动填充默认值，处理 YAML 友好的字段命名。

        Args:
            ui_data: ui 部分的原始数据。
            file_path: 源文件路径（用于日志）。

        Returns:
            ModuleUISchema 或 None。
        """
        try:
            identity = self._parse_identity(ui_data.get("identity", {}))
            if identity is None:
                logger.warning("identity 解析失败: %s", file_path)
                return None

            actions = self._parse_actions(ui_data.get("actions", []))
            rendering = self._parse_rendering(ui_data.get("rendering", {}))
            clients = self._parse_clients(ui_data.get("clients", {}))

            return ModuleUISchema(
                identity=identity,
                actions=actions,
                rendering=rendering,
                clients=clients,
            )

        except Exception as exc:
            logger.warning("UI Schema 解析失败: %s | %s", file_path, exc)
            return None

    def _parse_identity(self, data: dict[str, Any]) -> ModuleIdentity | None:
        """解析 identity 部分。

        Args:
            data: identity 原始数据。

        Returns:
            ModuleIdentity 或 None。
        """
        if not isinstance(data, dict):
            return None
        if not data.get("id") or not data.get("name"):
            return None
        try:
            return ModuleIdentity(**data)
        except Exception:
            return None

    def _parse_actions(self, data: Any) -> list[ModuleAction]:
        """解析 actions 列表。

        Args:
            data: actions 原始数据。

        Returns:
            ModuleAction 列表。
        """
        if not isinstance(data, list):
            return []
        actions: list[ModuleAction] = []
        for item in data:
            if isinstance(item, dict):
                try:
                    actions.append(ModuleAction(**item))
                except Exception as exc:
                    logger.debug("跳过无效 action: %s", exc)
        return actions

    def _parse_rendering(self, data: Any) -> ModuleRendering:
        """解析 rendering 部分。

        Args:
            data: rendering 原始数据。

        Returns:
            ModuleRendering 实例。
        """
        if not isinstance(data, dict):
            return ModuleRendering()

        chat_data = data.get("chat", [])
        chat_list: list[ChatInteractionConfig] = []
        if isinstance(chat_data, list):
            for item in chat_data:
                if isinstance(item, dict):
                    try:
                        chat_list.append(ChatInteractionConfig(**item))
                    except Exception as exc:
                        logger.debug("跳过无效 chat interaction: %s", exc)

        spaces_data = data.get("spaces", [])
        spaces_list: list[RenderingSpaceConfig] = []
        if isinstance(spaces_data, list):
            for item in spaces_data:
                if isinstance(item, dict):
                    try:
                        spaces_list.append(RenderingSpaceConfig(**item))
                    except Exception as exc:
                        logger.debug("跳过无效 space config: %s", exc)

        return ModuleRendering(
            chat=chat_list,
            spaces=spaces_list,
            dock=data.get("dock"),
            fullscreen=data.get("fullscreen"),
        )

    def _parse_clients(self, data: Any) -> ClientCapabilities:
        """解析 clients 部分。

        Args:
            data: clients 原始数据。

        Returns:
            ClientCapabilities 实例。
        """
        if not isinstance(data, dict):
            return ClientCapabilities()
        try:
            return ClientCapabilities(**data)
        except Exception:
            return ClientCapabilities()

    def _extract_and_cache_data(self, raw: dict[str, Any], data_section: dict[str, Any]) -> None:
        """从 YAML 原始数据中提取 data 段并缓存。

        优先使用 ui.identity.id 作为模块 ID，其次使用顶层 config_id。

        Args:
            raw: YAML 文件的完整原始数据。
            data_section: data 段的内容。
        """
        # 尝试从 ui.identity.id 获取 module_id
        module_id: str | None = None
        ui_data = raw.get("ui")
        if isinstance(ui_data, dict):
            identity = ui_data.get("identity")
            if isinstance(identity, dict):
                module_id = identity.get("id")

        # 回退到顶层 config_id
        if not module_id:
            module_id = raw.get("config_id")

        if module_id and isinstance(module_id, str):
            self._data_decls[module_id] = data_section
            logger.debug(
                "缓存 data 声明: module=%s, collections=%s",
                module_id,
                list(data_section.keys()),
            )
