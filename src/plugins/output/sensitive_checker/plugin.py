"""敏感数据检查 Output 插件。

负责在管道循环的输出阶段扫描工具执行结果中的敏感数据模式，
对检测到的敏感信息进行脱敏处理（替换为 ***）。

支持的敏感数据模式：
    - OpenAI API Key
    - GitHub Token
    - Slack Token
    - 密码字段
    - API Key 字段
    - AWS Access Key

State 命名空间：
    - sensitive_detected : 是否检测到敏感数据
    - tool_results : 脱敏后的工具执行结果（原地更新）
"""

from __future__ import annotations

import logging
import re
from typing import Any

from pipeline.plugin import IOutputPlugin, OutputResult, PluginContext
from pipeline.types import ErrorPolicy, StateKeys

logger = logging.getLogger(__name__)


class SensitiveChecker(IOutputPlugin):
    """敏感数据检查 Output 插件。

    扫描工具执行结果中的敏感数据模式（API Key、Token、密码等），
    对检测到的内容进行脱敏处理，防止敏感信息泄露。

    脱敏策略：将匹配到的敏感值替换为 ***。

    优先级：20（安全级，优先执行）
    错误策略：SKIP（脱敏失败不阻塞管道）

    Attributes:
        _config: 插件配置字典
    """

    error_policy = ErrorPolicy.SKIP

    _SENSITIVE_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
        ("OpenAI Key", re.compile(r"sk-[a-zA-Z0-9]{20,}")),
        ("GitHub Token", re.compile(r"ghp_[a-zA-Z0-9]{36}")),
        ("Slack Token", re.compile(r"xoxb-[a-zA-Z0-9-]+")),
        ("AWS Access Key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ]

    _SENSITIVE_KEY_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
        (
            "Password Field",
            re.compile(
                r"(password|passwd|pwd|secret)",
                re.IGNORECASE,
            ),
        ),
        (
            "API Key Field",
            re.compile(
                r"(api_key|apikey|api-key)",
                re.IGNORECASE,
            ),
        ),
    ]

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        """初始化敏感数据检查插件。

        Args:
            config: 插件配置字典，支持以下键：
                - enabled: 是否启用检查（默认 True）
                - mask: 脱敏替换字符串（默认 ***）
        """
        self._config = config or {}
        self._enabled = self._config.get("enabled", True)
        self._mask = self._config.get("mask", "***")

    @property
    def name(self) -> str:
        """插件唯一标识名称。"""
        return "sensitive_checker"

    @property
    def priority(self) -> int:
        """插件执行优先级。"""
        return self._config.get("priority", 20)

    async def execute(self, ctx: PluginContext) -> OutputResult:
        """执行敏感数据检查。

        扫描 tool_results 中的所有字符串内容，检测敏感数据模式
        并进行脱敏处理。

        Args:
            ctx: 插件执行上下文

        Returns:
            包含脱敏结果和检测标记的输出结果
        """
        if not self._enabled:
            return OutputResult()

        tool_results = ctx.state.get(StateKeys.TOOL_RESULTS, [])
        if not tool_results:
            return OutputResult()

        detected = False
        sanitized_results: list[Any] = []

        for result in tool_results:
            sanitized = self._sanitize_value(result)
            if sanitized is not result:
                detected = True
            sanitized_results.append(sanitized)

        state_updates: dict[str, Any] = {}
        if detected:
            state_updates[StateKeys.TOOL_RESULTS] = sanitized_results
            state_updates["sensitive_detected"] = True
            logger.info(
                "[%s] Sensitive data detected and masked",
                self.name,
            )

        return OutputResult(state_updates=state_updates)

    def _sanitize_value(self, value: Any) -> Any:
        """递归脱敏值中的敏感数据。

        对字符串进行模式匹配和脱敏，对字典和列表递归处理。

        Args:
            value: 待脱敏的值

        Returns:
            脱敏后的值
        """
        if isinstance(value, str):
            return self._sanitize_string(value)
        if isinstance(value, dict):
            sanitized = {}
            changed = False
            for k, v in value.items():
                new_v = self._sanitize_value(v)
                if new_v is not v:
                    changed = True
                sanitized[k] = new_v
            return sanitized if changed else value
        if isinstance(value, list):
            sanitized = []
            changed = False
            for item in value:
                new_item = self._sanitize_value(item)
                if new_item is not item:
                    changed = True
                sanitized.append(new_item)
            return sanitized if changed else value
        return value

    def _sanitize_string(self, text: str) -> str:
        """对字符串进行敏感数据模式匹配和脱敏。

        先检查 key=value 模式中的敏感 key，再检查独立 token 模式。

        Args:
            text: 待脱敏的字符串

        Returns:
            脱敏后的字符串
        """
        result = text

        for label, pattern in self._SENSITIVE_PATTERNS:
            if pattern.search(result):
                logger.debug(
                    "[%s] Detected %s pattern, masking",
                    self.name,
                    label,
                )
                result = pattern.sub(self._mask, result)

        for label, key_pattern in self._SENSITIVE_KEY_PATTERNS:
            value_pattern = re.compile(
                r"(" + key_pattern.pattern + r")\s*[=:]\s*(['\"]?)(\S+?)(['\"]?\s*)",
                flags=key_pattern.flags,
            )
            if value_pattern.search(result):
                logger.debug(
                    "[%s] Detected %s, masking value",
                    self.name,
                    label,
                )
                result = value_pattern.sub(
                    rf"\1\2{self._mask}\4",
                    result,
                )

        return result
