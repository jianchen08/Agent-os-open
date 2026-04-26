"""
工作空间感知 Mixin

暴露接口：
- WorkspaceAwareMixin：统一 workspace 消费的 Mixin 类
"""

from pathlib import Path
from typing import Any


class WorkspaceAwareMixin:
    """工作空间感知 Mixin，统一管理工具的 workspace 消费逻辑。

    提供路径解析、项目根推断、工作目录获取等通用能力，
    各工具通过继承此 Mixin 即可获得一致的 workspace 处理行为。
    """

    def _init_workspace(self, inputs: dict[str, Any]) -> None:
        """从输入参数初始化工作空间和项目根路径。

        优先使用 inputs 中显式传入的 workspace / project_root，
        缺省时分别回退到当前工作目录和自动推断。

        Args:
            inputs: 工具执行时接收的输入参数字典。
        """
        if inputs.get("workspace"):
            self._workspace: Path = Path(inputs["workspace"])
        elif inputs.get("project_root"):
            self._workspace: Path = Path(inputs["project_root"])
        else:
            self._workspace: Path = Path.cwd()

        if inputs.get("project_root"):
            self._project_root: Path = Path(inputs["project_root"])
        else:
            self._project_root = self._infer_project_root(self._workspace)

    def resolve_path(self, path_str: str) -> Path:
        """解析路径，处理绝对路径、相对路径及前缀去重。

        绝对路径直接返回；相对路径与 self._workspace 拼接。
        当相对路径已包含 workspace 的完整路径或尾部组件前缀时自动去重，
        避免产生类似 workspace/workspace/file 的重复路径。

        去重策略：
        1. 完整匹配：路径字符串以 workspace 完整路径开头
        2. 尾部匹配：路径字符串以 workspace 路径的尾部组件开头

        Args:
            path_str: 待解析的路径字符串。

        Returns:
            解析后的 Path 对象（已 resolve）。
        """
        path = Path(path_str)
        if path.is_absolute():
            return path.resolve()

        normalized_path = str(path).replace("\\", "/")
        normalized_ws = str(self._workspace).replace("\\", "/")

        # 完整路径前缀匹配
        if normalized_path == normalized_ws:
            return self._workspace.resolve()
        if normalized_path.startswith(normalized_ws + "/"):
            relative_part = normalized_path[len(normalized_ws) + 1:]
            return (self._workspace / relative_part).resolve()

        # 尾部组件前缀匹配，逐级缩短 workspace 后缀进行比对
        ws_parts = normalized_ws.split("/")
        for i in range(1, len(ws_parts)):
            suffix = "/".join(ws_parts[i:])
            if not suffix:
                continue
            if normalized_path == suffix:
                return self._workspace.resolve()
            if normalized_path.startswith(suffix + "/"):
                relative_part = normalized_path[len(suffix) + 1:]
                return (self._workspace / relative_part).resolve()

        return (self._workspace / path).resolve()

    def _format_output_path(self, resolved_path: Path, original_input: str) -> str:
        """将解析后的路径按输入格式返回。

        输入是相对路径 → 返回相对路径（相对于项目根）
        输入是绝对路径 → 返回绝对路径

        Args:
            resolved_path: 通过 resolve_path 解析后的绝对路径
            original_input: 用户原始输入的路径字符串

        Returns:
            格式化后的路径字符串
        """
        original = Path(original_input)
        if original.is_absolute():
            return str(resolved_path)

        try:
            return str(resolved_path.relative_to(self._project_root))
        except ValueError:
            try:
                return str(resolved_path.relative_to(self._workspace))
            except ValueError:
                return str(resolved_path)

    def get_working_dir(self, inputs: dict[str, Any]) -> Path | None:
        """获取当前工具的工作目录。

        优先级：inputs 中显式传入的 working_dir > self._workspace。

        Args:
            inputs: 工具执行时接收的输入参数字典。

        Returns:
            工作目录的 Path 对象，均无可用时返回 None。
        """
        working_dir = inputs.get("working_dir")
        if working_dir:
            return Path(working_dir)
        return getattr(self, "_workspace", None)

    @staticmethod
    def _infer_project_root(workspace: Path) -> Path:
        """从工作空间路径推断项目根目录。

        若 workspace 自身包含 .git 目录则视为项目根；
        否则逐级向上查找，直到遇到含 .git 的祖先目录；
        均未找到时直接返回 workspace 本身。

        Args:
            workspace: 工作空间路径。

        Returns:
            推断出的项目根目录 Path 对象。
        """
        candidate = workspace.resolve()
        for _ in range(20):
            if (candidate / ".git").exists():
                return candidate
            parent = candidate.parent
            if parent == candidate:
                break
            candidate = parent
        return workspace.resolve()
