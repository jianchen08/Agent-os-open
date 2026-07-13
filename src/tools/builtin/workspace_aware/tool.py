"""
工作空间感知 Mixin

暴露接口：
- WorkspaceAwareMixin：统一 workspace 消费的 Mixin 类
"""

import platform
import re
from pathlib import Path
from typing import Any


class WorkspaceAwareMixin:
    """工作空间感知 Mixin，统一管理工具的 workspace 消费逻辑。

    提供路径解析、项目根推断、工作目录获取等通用能力，
    各工具通过继承此 Mixin 即可获得一致的 workspace 处理行为。

    新增统一的路径权限校验入口 check_path_allowed，
    所有工具无需各自实现 workspace 范围检查，调用此方法即可
    按 permission_policies 声明（root_task / subtask / default）决策。
    """

    # ── 权限策略管理器（模块级缓存，避免每次调用重新解析配置）──
    _policy_manager = None

    @classmethod
    def _get_policy_manager(cls):
        """获取缓存的 PermissionPolicyManager 单例（从配置文件加载策略）。"""
        if cls._policy_manager is None:
            from isolation.permission_policy import PermissionPolicyManager  # noqa: PLC0415

            cls._policy_manager = PermissionPolicyManager()
        return cls._policy_manager

    def check_path_allowed(
        self,
        path: str,
        operation: str = "read",
        agent_level: int | str | None = None,
    ) -> tuple[bool, str]:
        """统一的路径权限校验入口。

        根据 agent 层级选取对应策略（L1/缺省→root_task, L2+→subtask），
        再按操作类型（read/write）调用 PermissionChecker 决策。
        通过返回 (True, "")，拒绝返回 (False, 错误原因)。

        Args:
            path: 待校验的文件路径（绝对路径或相对于 project_root 的相对路径）
            operation: "read" 或 "write"
            agent_level: 调用方 agent 层级（1=主agent, 2+=子任务, None=按L1处理）

        Returns:
            (通过与否, 错误描述)
        """
        # 确保 workspace/project_root 已初始化
        workspace = getattr(self, "_workspace", None)
        project_root = getattr(self, "_project_root", None)
        if workspace is None or project_root is None:
            return False, "workspace 未初始化，无法校验路径权限"

        policy_manager = self._get_policy_manager()
        policy_name = policy_manager.get_policy_name_for_agent_level(agent_level)
        policy = policy_manager.get_policy(policy_name)

        from isolation.permission_checker import PermissionChecker  # noqa: PLC0415

        checker = PermissionChecker(str(project_root))

        if operation == "write":
            ok, err = checker.check_write_permission(
                path,
                str(workspace),
                policy,
            )
        else:
            ok, err = checker.check_read_permission(
                path,
                str(workspace),
                policy,
            )
        return ok, err

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
        elif getattr(self, "base_path", None):
            self._workspace: Path = self.base_path
        else:
            self._workspace: Path = Path.cwd()

        if inputs.get("project_root"):
            self._project_root: Path = Path(inputs["project_root"])
        else:
            self._project_root = self._infer_project_root(self._workspace)

    def _init_agent_level(self, inputs: dict[str, Any]) -> None:
        """从输入参数初始化 agent 层级，供路径权限校验决策使用。

        读取 inputs["parent_agent_level"]（默认 1），解析为整数存到
        self._agent_level；解析失败回退到 1（主 agent）。

        Args:
            inputs: 工具执行时接收的输入参数字典。
        """
        raw_level = inputs.get("parent_agent_level", 1)
        try:
            self._agent_level = int(str(raw_level).upper().lstrip("L"))
        except (ValueError, TypeError):
            self._agent_level = 1

    def resolve_path(self, path_str: str) -> Path:  # noqa: PLR0911
        """解析路径，处理绝对路径、相对路径及前缀去重。

        绝对路径直接返回；相对路径与 self._workspace 拼接。
        当相对路径已包含 workspace 的完整路径或尾部组件前缀时自动去重，
        避免产生类似 workspace/workspace/file 的重复路径。
        Windows 下额外处理 Git Bash 风格绝对路径（/d/path → D:\\path）。
        """
        # Windows: 转换 Git Bash 风格绝对路径 (/d/path → D:\path)
        if platform.system() == "Windows":
            normalized = path_str.replace("\\", "/")
            drive_match = re.match(r"^/([a-zA-Z])/(.+)", normalized)
            if drive_match:
                drive = drive_match.group(1).upper()
                rest = drive_match.group(2)
                return Path(f"{drive}:\\{rest}").resolve()

        path = Path(path_str)
        if path.is_absolute():
            return path.resolve()

        normalized_path = str(path).replace("\\", "/")
        normalized_ws = str(self._workspace).replace("\\", "/")

        # 完整路径前缀匹配
        if normalized_path == normalized_ws:
            return self._workspace.resolve()
        if normalized_path.startswith(normalized_ws + "/"):
            relative_part = normalized_path[len(normalized_ws) + 1 :]
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
                relative_part = normalized_path[len(suffix) + 1 :]
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
