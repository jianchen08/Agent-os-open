#!/usr/bin/env python3
"""插件脚手架生成器。

一键生成符合插件开发标准规范的插件骨架代码。

用法:
    python -m tools.plugin_scaffold <plugin_type> <plugin_name> [--desc DESCRIPTION] [--priority PRIORITY]

示例:
    python -m tools.plugin_scaffold input my_feature --desc "我的新功能插件"
    python -m tools.plugin_scaffold output result_handler --desc "结果处理器" --priority 50
    python -m tools.plugin_scaffold core my_core --desc "自定义核心插件"
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# 项目根目录（假设此文件在 tools/ 下）
PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCAFFOLD_DIR = PROJECT_ROOT / "config" / "templates" / "plugin_scaffold"
PLUGINS_DIR = PROJECT_ROOT / "src" / "plugins"

VALID_TYPES = {"input", "output", "core"}


def to_camel_case(snake: str) -> str:
    """将 snake_case 转换为 CamelCase。"""
    return "".join(word.capitalize() for word in snake.split("_"))


def validate_plugin_name(name: str) -> None:
    """验证插件名称符合规范。"""
    if not re.match(r"^[a-z][a-z0-9_]*$", name):
        print(f"错误: 插件名称 '{name}' 不符合 snake_case 规范")
        print("  - 必须以小写字母开头")
        print("  - 只能包含小写字母、数字和下划线")
        sys.exit(1)

    if name.endswith("_"):
        print(f"错误: 插件名称 '{name}' 不能以下划线结尾")
        sys.exit(1)


def validate_plugin_type(plugin_type: str) -> None:
    """验证插件类型。"""
    if plugin_type not in VALID_TYPES:
        print(f"错误: 插件类型 '{plugin_type}' 无效")
        print(f"  - 有效类型: {', '.join(sorted(VALID_TYPES))}")
        sys.exit(1)


def check_existing(plugin_type: str, plugin_name: str) -> None:
    """检查插件是否已存在。"""
    target_dir = PLUGINS_DIR / plugin_type / plugin_name
    if target_dir.exists():
        print(f"错误: 插件目录已存在: {target_dir}")
        sys.exit(1)


def load_template(plugin_type: str) -> str:
    """加载对应类型的模板文件。"""
    template_map = {
        "input": "input_plugin.py",
        "output": "output_plugin.py",
        "core": "core_plugin.py",
    }
    template_file = SCAFFOLD_DIR / template_map[plugin_type]
    if not template_file.exists():
        print(f"错误: 模板文件不存在: {template_file}")
        sys.exit(1)
    return template_file.read_text(encoding="utf-8")


def fill_template(
    template: str,
    plugin_name: str,
    plugin_type: str,
    description: str,
    priority: int,
) -> str:
    """填充模板占位符。"""
    plugin_class = to_camel_case(plugin_name)
    if plugin_type == "core":
        plugin_class = plugin_class + "Core"
    else:
        plugin_class = plugin_class + "Plugin"

    replacements = {
        "{plugin_name}": plugin_name,
        "{PluginClass}": plugin_class,
        "{plugin_type}": plugin_type,
        "{one_line_description}": description,
        "{detailed_description}": description + "。",
        "{priority_value}": str(priority),
        "{read_state_keys}": "TODO: 列出读取的 state 键",
        "{write_state_keys}": "TODO: 列出写入的 state 键",
        "{other_config_docs}": "TODO: 列出其他配置项",
        "{config_docs}": "TODO: 列出配置项",
        # 双花括号还原（模板中的 {{}} 是字面量）
    }

    result = template
    for key, value in replacements.items():
        result = result.replace(key, value)

    # 还原模板中的双花括号转义
    result = result.replace("{{}}", "{}")
    result = result.replace("{{", "{")
    result = result.replace("}}", "}")

    return result


def create_init_file(plugin_name: str, plugin_type: str) -> str:
    """生成 __init__.py 内容。"""
    plugin_class = to_camel_case(plugin_name)
    if plugin_type == "core":
        plugin_class = plugin_class + "Core"
    else:
        plugin_class = plugin_class + "Plugin"

    return (
        f'"""{plugin_name} 插件 — 自动生成的模块入口。"""\n\n'
        f"from plugins.{plugin_type}.{plugin_name}.{plugin_name} import {plugin_class}\n\n"
        f'__all__ = ["{plugin_class}"]\n'
    )


def create_test_file(plugin_name: str, plugin_type: str, plugin_class: str) -> str:
    """生成测试文件内容。"""
    return f'''"""{{plugin_name}} 插件测试。"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from pipeline.plugin import PluginContext, PluginResult, OutputResult


def make_ctx(state: dict | None = None, config: dict | None = None) -> PluginContext:
    """创建测试用 PluginContext。"""
    return PluginContext(
        state=state or {{}},
        config=config or {{}},
        _services={{}},
    )


class Test{plugin_class}:
    """{{plugin_class}} 测试类。"""

    def _make_plugin(self, config: dict | None = None) -> {plugin_class}:
        """创建插件实例。"""
        from plugins.{plugin_type}.{plugin_name}.{plugin_name} import {plugin_class}
        return {plugin_class}(config=config)

    def test_name_property(self) -> None:
        """测试 name 属性。"""
        plugin = self._make_plugin()
        assert plugin.name == "{plugin_name}"

    def test_priority_property(self) -> None:
        """测试 priority 属性。"""
        plugin = self._make_plugin()
        assert isinstance(plugin.priority, int)
        assert plugin.priority >= 0

    def test_default_config(self) -> None:
        """测试默认配置（config=None）。"""
        plugin = self._make_plugin(config=None)
        assert plugin._enabled is True

    @pytest.mark.asyncio
    async def test_execute_disabled(self) -> None:
        """测试插件禁用时的行为。"""
        plugin = self._make_plugin(config={{"enabled": False}})
        ctx = make_ctx()
        result = await plugin.execute(ctx)
        assert result.state_updates == {{}}

    @pytest.mark.asyncio
    async def test_execute_basic(self) -> None:
        """测试基本执行路径。"""
        plugin = self._make_plugin()
        ctx = make_ctx(state={{"key": "value"}})
        result = await plugin.execute(ctx)
        # TODO: 添加具体的断言
        assert result is not None
'''


def generate_scaffold(
    plugin_type: str,
    plugin_name: str,
    description: str,
    priority: int,
) -> Path:
    """生成插件骨架。

    Args:
        plugin_type: 插件类型 (input/output/core)
        plugin_name: 插件名称 (snake_case)
        description: 插件描述
        priority: 优先级

    Returns:
        生成的插件目录路径
    """
    # 验证
    validate_plugin_name(plugin_name)
    validate_plugin_type(plugin_type)
    check_existing(plugin_type, plugin_name)

    # 创建目录
    target_dir = PLUGINS_DIR / plugin_type / plugin_name
    target_dir.mkdir(parents=True, exist_ok=False)

    # 生成主文件
    template = load_template(plugin_type)
    main_content = fill_template(template, plugin_name, plugin_type, description, priority)
    main_file = target_dir / f"{plugin_name}.py"
    main_file.write_text(main_content, encoding="utf-8")

    # 生成 __init__.py
    init_content = create_init_file(plugin_name, plugin_type)
    init_file = target_dir / "__init__.py"
    init_file.write_text(init_content, encoding="utf-8")

    # 生成测试文件
    plugin_class = to_camel_case(plugin_name)
    if plugin_type == "core":
        plugin_class += "Core"
    else:
        plugin_class += "Plugin"

    test_content = create_test_file(plugin_name, plugin_type, plugin_class)
    test_dir = target_dir / "tests"
    test_dir.mkdir(exist_ok=True)
    (test_dir / "__init__.py").write_text("", encoding="utf-8")
    (test_dir / f"test_{plugin_name}.py").write_text(test_content, encoding="utf-8")

    return target_dir


def main() -> None:
    """命令行入口。"""
    parser = argparse.ArgumentParser(
        description="插件脚手架生成器 — 一键生成符合规范的插件骨架",
    )
    parser.add_argument(
        "plugin_type",
        choices=sorted(VALID_TYPES),
        help="插件类型: input, output, core",
    )
    parser.add_argument(
        "plugin_name",
        help="插件名称 (snake_case 格式，如 my_feature)",
    )
    parser.add_argument(
        "--desc",
        default="自动生成的插件",
        help="插件描述 (默认: '自动生成的插件')",
    )
    parser.add_argument(
        "--priority",
        type=int,
        default=50,
        help="插件优先级 (默认: 50)",
    )

    args = parser.parse_args()

    print(f"正在生成插件骨架...")
    print(f"  类型: {args.plugin_type}")
    print(f"  名称: {args.plugin_name}")
    print(f"  描述: {args.desc}")
    print(f"  优先级: {args.priority}")

    target_dir = generate_scaffold(
        plugin_type=args.plugin_type,
        plugin_name=args.plugin_name,
        description=args.desc,
        priority=args.priority,
    )

    print(f"\n✅ 插件骨架已生成: {target_dir}")
    print(f"\n生成的文件:")
    for f in sorted(target_dir.rglob("*")):
        if f.is_file():
            print(f"  {f.relative_to(target_dir)}")

    print(f"\n下一步:")
    print(f"  1. 编辑 {target_dir / f'{args.plugin_name}.py'} 实现插件逻辑")
    print(f"  2. 编辑测试文件完善测试用例")
    print(f"  3. 在 config/pipelines/default.yaml 中注册插件")
    print(f"  4. 运行 plugin_validator 验证插件合规性")


if __name__ == "__main__":
    main()
