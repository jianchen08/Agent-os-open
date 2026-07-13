# -*- coding: utf-8 -*-
"""
收集所有工具信息并生成配置文件

此脚本会扫描所有工具代码，提取工具定义，
然后生成与代码一致的工具配置文件。
"""

import asyncio
import sys
from pathlib import Path

# 添加项目根目录到路径
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

import yaml


async def collect_all_tools():
    """收集所有工具信息"""

    # 导入工具模块
    from src.tools.builtin import get_all_builtin_tools, get_all_builtin_tools_with_session

    tools_info = []
    cache_configs = {}

    # 1. 收集不需要 session 的工具
    print("正在收集不需要 session 的工具...")
    for tool_item in get_all_builtin_tools():
        if hasattr(tool_item, 'get_tool_definition'):
            tool_def = tool_item.get_tool_definition()
            tools_info.append({
                'name': tool_def.name,
                'description': tool_def.description,
                'category': tool_def.category.value if tool_def.category else 'system',
                'level': tool_def.level.value,
                'requires_approval': tool_def.requires_approval,
                'dangerous_operations': tool_def.dangerous_operations,
                'tags': tool_def.tags,
            })

            # 生成缓存配置
            cache_configs[tool_def.name] = generate_cache_config(tool_def)
            print(f"  ✓ {tool_def.name}")

    # 2. 收集需要 session 的工具（只获取定义，不实例化）
    print("\n正在收集需要 session 的工具...")
    session_tool_classes = get_all_builtin_tools_with_session()
    for tool_class in session_tool_classes:
        try:
            # 创建临时实例来获取定义（某些工具可能需要特殊处理）
            if tool_class.__name__ == 'TaskSubmitTool':
                tool_instance = tool_class(session=None)
            elif tool_class.__name__ == 'TaskTool':
                tool_instance = tool_class(session=None)
            elif tool_class.__name__ == 'TaskEvaluateTool':
                tool_instance = tool_class(session=None)
            elif tool_class.__name__ == 'MemoryTool':
                tool_instance = tool_class(session=None)
            else:
                tool_instance = tool_class()

            tool_def = tool_instance.get_tool_definition()
            tools_info.append({
                'name': tool_def.name,
                'description': tool_def.description,
                'category': tool_def.category.value if tool_def.category else 'system',
                'level': tool_def.level.value,
                'requires_approval': tool_def.requires_approval,
                'dangerous_operations': tool_def.dangerous_operations,
                'tags': tool_def.tags,
            })

            # 生成缓存配置
            cache_configs[tool_def.name] = generate_cache_config(tool_def)
            print(f"  ✓ {tool_def.name}")
        except Exception as e:
            print(f"  ✗ {tool_class.__name__}: {e}")

    return tools_info, cache_configs


def generate_cache_config(tool_def):
    """根据工具定义生成缓存配置"""

    # 默认不缓存
    config = {'enabled': False}

    # 根据类别决定是否缓存
    if tool_def.category:
        category = tool_def.category.value if hasattr(tool_def.category, 'value') else str(tool_def.category)

        # 文件操作类 - 缓存读取操作
        if category == 'file':
            config = {
                'enabled': True,
                'ttl': 300,
                'invalidate_on': 'file_change',
                'cacheable_operations': ['read']
            }
        # 搜索类 - 缓存较长时间
        elif category == 'search':
            config = {
                'enabled': True,
                'ttl': 600
            }
        # Web 类 - 缓存较长时间
        elif category == 'web':
            config = {
                'enabled': True,
                'ttl': 1800
            }
        # 任务、执行类 - 不缓存
        elif category in ['task', 'execution', 'system']:
            config = {'enabled': False}
        # 其他 - 默认缓存5分钟
        else:
            config = {
                'enabled': True,
                'ttl': 300
            }

    # 如果有危险操作或需要审批，不缓存
    if tool_def.requires_approval or tool_def.dangerous_operations:
        config = {'enabled': False}

    return config


def generate_config_file(tools_info, cache_configs):
    """生成配置文件"""

    # 按类别分组
    [t for t in tools_info if t['level'] == 'system']
    [t for t in tools_info if t['level'] != 'system']

    config = {
        'tool_cache': {
            'enabled': True,
            'default_ttl': 300,
            'tools': cache_configs
        },
        'tools': tools_info,
        'permission_policies': {
            'admin': {
                'can_approve': True,
                'auto_approve_tools': ['*']
            },
            'developer': {
                'can_approve': False,
                'auto_approve_tools': [t['name'] for t in tools_info if not t['requires_approval'] and not t['dangerous_operations']],
                'require_approval_tools': [t['name'] for t in tools_info if t['requires_approval'] or t['dangerous_operations']]
            },
            'readonly': {
                'can_approve': False,
                'auto_approve_tools': [t['name'] for t in tools_info if t['category'] in ['search', 'web'] and not t['requires_approval']],
                'require_approval_tools': [t['name'] for t in tools_info if t['category'] not in ['search', 'web'] or t['requires_approval']]
            }
        }
    }

    return config


async def main():
    """主函数"""
    print("=" * 60)
    print("工具配置收集器")
    print("=" * 60)

    # 收集工具信息
    tools_info, cache_configs = await collect_all_tools()

    print(f"\n共收集到 {len(tools_info)} 个工具")

    # 生成配置
    config = generate_config_file(tools_info, cache_configs)

    # 写入文件
    config_path = project_root / 'config' / 'tools' / 'builtin_tools_config.yaml'
    config_path.parent.mkdir(parents=True, exist_ok=True)

    with open(config_path, 'w', encoding='utf-8') as f:
        f.write("# -*- coding: utf-8 -*-\n")
        f.write("# 内置工具配置\n")
        f.write("# 此文件由 scripts/tools/collect_tool_info.py 自动生成\n")
        f.write("# 请勿手动修改，运行脚本重新生成\n\n")
        yaml.dump(config, f, allow_unicode=True, sort_keys=False, default_flow_style=False)

    print(f"\n✅ 配置文件已生成: {config_path}")

    # 打印统计信息
    print("\n" + "=" * 60)
    print("统计信息")
    print("=" * 60)
    print(f"总工具数: {len(tools_info)}")
    print(f"系统级工具: {len([t for t in tools_info if t['level'] == 'system'])}")
    print(f"用户级工具: {len([t for t in tools_info if t['level'] != 'system'])}")
    print(f"需要审批的工具: {len([t for t in tools_info if t['requires_approval']])}")
    print(f"启用缓存的工具: {len([c for c in cache_configs.values() if c.get('enabled')])}")

    # 打印工具列表
    print("\n" + "=" * 60)
    print("工具列表")
    print("=" * 60)
    for tool in sorted(tools_info, key=lambda x: x['name']):
        cache_status = "🔄" if cache_configs.get(tool['name'], {}).get('enabled') else "❌"
        approval_status = "⚠️" if tool['requires_approval'] else "✓"
        print(f"{approval_status} {cache_status} {tool['name']:<30} [{tool['category']:<12}] {tool['level']}")


if __name__ == '__main__':
    asyncio.run(main())
