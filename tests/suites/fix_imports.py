#!/usr/bin/env python3
"""修复 import 语句：从 agent_os.xxx 改为直接模块引用"""

import re
from pathlib import Path

def fix_imports_in_file(filepath: Path) -> int:
    """修复单个文件中的 import 语句，返回修改数量"""
    try:
        content = filepath.read_text(encoding='utf-8')
    except Exception as e:
        print(f"  跳过 {filepath}: {e}")
        return 0

    changes = 0

    # 处理 from agent_os.xxx import yyy
    # 变成 from xxx import yyy（去掉 agent_os. 前缀）
    content, n = re.subn(
        r'from agent_os\.(\w)',
        r'from \1',
        content
    )
    changes += n

    # 处理 import agent_os.xxx
    # 变成 import xxx
    content, n = re.subn(
        r'import agent_os\.(\w)',
        r'import \1',
        content
    )
    changes += n

    # 处理 agent_os.channels.cli.cli_main:main 这种引用
    content, n = re.subn(
        r'agent_os\.channels\.cli\.cli_main',
        r'channels.cli.cli_main',
        content
    )
    changes += n

    # 处理字符串中的引用如 "agent_os.xxx"
    content, n = re.subn(
        r'"agent_os\.(\w)',
        r'"\1',
        content
    )
    changes += n

    if changes > 0:
        filepath.write_text(content, encoding='utf-8')
        print(f"  修改 {filepath}: {changes} 处")

    return changes


def main():
    src_dir = Path("d:/Jianguoyun/Agent os/src")

    total_changes = 0
    files_modified = 0

    for py_file in src_dir.rglob("*.py"):
        # 跳过 __pycache__
        if "__pycache__" in str(py_file):
            continue

        changes = fix_imports_in_file(py_file)
        if changes > 0:
            total_changes += changes
            files_modified += 1

    print(f"\n完成！修改了 {files_modified} 个文件，共 {total_changes} 处")


if __name__ == "__main__":
    main()
