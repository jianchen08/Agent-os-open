"""模板渲染器。

将模板中的占位符替换为变量值，支持保留 HTML 注释块和处理缺失变量。

公共 API:
    TemplateRenderer: 模板渲染器类
"""

import re
from typing import Any

from .types import TemplateSpec


class TemplateRenderer:
    """模板渲染器。

    支持：
    - 替换 ``{xxx}`` 占位符为变量值
    - 保留 HTML 注释块（不替换元数据区域内的占位符）
    - 可配置的缺失变量处理策略
    """

    def render(
        self,
        template: TemplateSpec,
        variables: dict[str, Any],
        *,
        strict: bool = False,
    ) -> str:
        """渲染模板，替换占位符为变量值。

        Args:
            template: 模板规格。
            variables: 变量字典，键为占位符名称，值为替换内容。
            strict: 严格模式。为 True 时，缺失变量抛出 KeyError；
                    为 False 时，保留原始占位符。

        Returns:
            渲染后的内容字符串。

        Raises:
            KeyError: strict 模式下存在缺失变量。
        """
        content = template.raw_content

        # 先检查缺失变量
        if strict:
            missing = [
                p for p in template.placeholders if p not in variables
            ]
            if missing:
                raise KeyError(
                    f"缺失模板变量: {', '.join(missing)}"
                )

        # 分离 HTML 注释块和正文
        parts = self._split_comment_blocks(content)

        # 只替换正文部分的占位符
        rendered_parts: list[str] = []
        for is_comment, text in parts:
            if is_comment:
                rendered_parts.append(text)
            else:
                rendered_parts.append(
                    self._replace_placeholders(text, variables, strict)
                )

        return "".join(rendered_parts)

    def _split_comment_blocks(self, content: str) -> list[tuple[bool, str]]:
        """将内容分割为注释块和正文交替段。

        Args:
            content: 完整 Markdown 内容。

        Returns:
            列表，每项为 (是否为注释块, 文本) 元组。
        """
        parts: list[tuple[bool, str]] = []
        last_end = 0

        for match in re.finditer(r"<!--.*?-->", content, re.DOTALL):
            # 注释块之前的正文
            if match.start() > last_end:
                parts.append((False, content[last_end : match.start()]))

            # 注释块
            parts.append((True, match.group(0)))
            last_end = match.end()

        # 最后一段正文
        if last_end < len(content):
            parts.append((False, content[last_end:]))

        return parts

    def _replace_placeholders(
        self,
        text: str,
        variables: dict[str, Any],
        strict: bool,
    ) -> str:
        """替换正文中的占位符。

        Args:
            text: 正文文本。
            variables: 变量字典。
            strict: 严格模式。

        Returns:
            替换后的文本。
        """

        def replacer(match: re.Match) -> str:
            name = match.group(1)
            if name in variables:
                return str(variables[name])
            if strict:
                raise KeyError(f"缺失模板变量: {name}")
            return match.group(0)  # 保留原始占位符

        return re.sub(r"\{(\w+)\}", replacer, text)
