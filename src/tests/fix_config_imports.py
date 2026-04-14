#!/usr/bin/env python3
"""修复配置文件中的 agent_os 引用"""

import re
from pathlib import Path

def fix_config_imports():
    """修复配置文件中的 import 引用"""
    src_dir = Path("d:/Jianguoyun/Agent os")

    # 配置文件 (yaml, md)
    for pattern in ["config/**/*.yaml", "config/**/*.yml", "src/**/*.md"]:
        for f in src_dir.glob(pattern):
            if "__pycache__" in str(f):
                continue
            try:
                content = f.read_text(encoding='utf-8')
            except:
                continue

            original = content
            # 修复 Python import 引用
            content = re.sub(r'from agent_os\.', 'from ', content)
            content = re.sub(r'import agent_os\.', 'import ', content)
            # 修复 YAML 中的 class 引用
            content = re.sub(r'agent_os\.plugins\.', 'plugins.', content)
            content = re.sub(r'agent_os\.channels\.', 'channels.', content)
            content = re.sub(r'agent_os\.pipeline\.', 'pipeline.', content)

            if content != original:
                f.write_text(content, encoding='utf-8')
                print(f"修改: {f}")

    # Python 测试文件
    for f in (src_dir / "tests").rglob("*.py"):
        try:
            content = f.read_text(encoding='utf-8')
        except:
            continue

        original = content
        content = re.sub(r'from agent_os\.', 'from ', content)
        content = re.sub(r'import agent_os\.', 'import ', content)

        if content != original:
            f.write_text(content, encoding='utf-8')
            print(f"修改测试: {f}")

    print("\n完成！")

if __name__ == "__main__":
    fix_config_imports()
