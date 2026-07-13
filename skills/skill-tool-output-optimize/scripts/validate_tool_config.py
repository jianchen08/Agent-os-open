# -*- coding: utf-8 -*-
"""
工具配置一致性验证脚本

验证工具代码中的定义与配置文件是否一致
"""

import asyncio
import sys
from pathlib import Path

# 添加项目根目录到路径
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

import yaml


async def validate_tool_config():
    """验证工具配置一致性"""

    print("=" * 70)
    print("工具配置一致性验证")
    print("=" * 70)

    # 1. 加载配置文件
    config_path = project_root / 'config' / 'tools' / 'builtin_tools_config.yaml'
    with open(config_path, encoding='utf-8') as f:
        config = yaml.safe_load(f)

    config_tools = {t['name']: t for t in config.get('tools', [])}
    config_cache_tools = config.get('tool_cache', {}).get('tools', {})

    # 2. 获取代码中定义的所有工具
    from src.tools.builtin import get_all_builtin_tools, get_all_builtin_tools_with_session

    code_tools = {}

    # 收集不需要 session 的工具
    for tool_item in get_all_builtin_tools():
        if hasattr(tool_item, 'get_tool_definition'):
            tool_def = tool_item.get_tool_definition()
            code_tools[tool_def.name] = tool_def

    # 收集需要 session 的工具
    for tool_class in get_all_builtin_tools_with_session():
        try:
            if tool_class.__name__ in ['TaskSubmitTool', 'TaskTool', 'TaskEvaluateTool', 'MemoryTool']:
                tool_instance = tool_class(session=None)
            else:
                tool_instance = tool_class()
            tool_def = tool_instance.get_tool_definition()
            code_tools[tool_def.name] = tool_def
        except Exception as e:
            print(f"⚠️  无法获取工具定义: {tool_class.__name__} - {e}")

    # 3. 验证一致性
    errors = []
    warnings = []

    # 检查1: 配置文件中的工具是否都在代码中存在
    for tool_name in config_tools.keys():
        if tool_name not in code_tools:
            errors.append(f"❌ 配置文件中的工具 '{tool_name}' 在代码中不存在")

    # 检查2: 代码中的工具是否都在配置文件中
    for tool_name in code_tools.keys():
        if tool_name not in config_tools:
            errors.append(f"❌ 代码中的工具 '{tool_name}' 不在配置文件中")

    # 检查3: 验证工具属性一致性
    for tool_name in code_tools.keys():
        if tool_name not in config_tools:
            continue

        code_def = code_tools[tool_name]
        config_def = config_tools[tool_name]

        # 检查 category
        code_category = code_def.category.value if code_def.category else None
        config_category = config_def.get('category')
        if code_category != config_category:
            errors.append(f"❌ 工具 '{tool_name}' 类别不一致: 代码={code_category}, 配置={config_category}")

        # 检查 level
        code_level = code_def.level.value
        config_level = config_def.get('level')
        if code_level != config_level:
            errors.append(f"❌ 工具 '{tool_name}' 级别不一致: 代码={code_level}, 配置={config_level}")

        # 检查 requires_approval
        code_approval = code_def.requires_approval
        config_approval = config_def.get('requires_approval')
        if code_approval != config_approval:
            errors.append(f"❌ 工具 '{tool_name}' 审批设置不一致: 代码={code_approval}, 配置={config_approval}")

    # 检查4: 缓存配置中的工具是否都存在
    for tool_name in config_cache_tools.keys():
        if tool_name not in code_tools:
            errors.append(f"❌ 缓存配置中的工具 '{tool_name}' 在代码中不存在")

    # 4. 输出结果
    print(f"\n📊 统计信息:")
    print(f"   代码中的工具数: {len(code_tools)}")
    print(f"   配置文件中的工具数: {len(config_tools)}")
    print(f"   缓存配置中的工具数: {len(config_cache_tools)}")

    if errors:
        print(f"\n❌ 发现 {len(errors)} 个错误:")
        for error in errors:
            print(f"   {error}")

    if warnings:
        print(f"\n⚠️  发现 {len(warnings)} 个警告:")
        for warning in warnings:
            print(f"   {warning}")

    if not errors and not warnings:
        print("\n✅ 验证通过！配置与代码完全一致。")
        return True
    elif not errors:
        print("\n✅ 验证通过，只有警告无错误。")
        return True
    else:
        print(f"\n❌ 验证失败，请修复上述错误。")
        return False


async def main():
    """主函数"""
    success = await validate_tool_config()
    sys.exit(0 if success else 1)


if __name__ == '__main__':
    asyncio.run(main())
