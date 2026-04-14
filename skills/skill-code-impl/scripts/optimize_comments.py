"""
批量优化 Python 代码注释脚本

按照模板格式优化文件头注释和函数注释：
- 文件头添加"暴露接口"部分
- 函数注释删除 Args/Returns，保留一行职责描述
"""

import ast
import re
from pathlib import Path
from typing import Any


def extract_file_info(file_path: str) -> dict[str, Any]:
    """提取文件的接口信息"""
    with open(file_path, 'r', encoding='utf-8') as f:
        content = f.read()

    try:
        tree = ast.parse(content)
    except SyntaxError:
        return {"has_error": True}

    interfaces = []
    functions = []
    classes = []

    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            if node.name.startswith('_'):
                continue
            # 获取函数签名
            args = []
            for arg in node.args.args:
                arg_str = arg.arg
                if arg.annotation:
                    arg_str += f": {ast.unparse(arg.annotation)}"
                args.append(arg_str)

            returns = ""
            if node.returns:
                returns = f" -> {ast.unparse(node.returns)}"

            func_name = node.name
            func_sig = f"{func_name}({', '.join(args)}){returns}"
            functions.append((func_name, func_sig))

        elif isinstance(node, ast.ClassDef):
            if node.name.startswith('_'):
                continue
            classes.append(node.name)

    return {
        "functions": functions,
        "classes": classes,
        "has_error": False
    }


def optimize_file_header(file_path: str, file_info: dict[str, Any]) -> bool:
    """优化文件头注释"""
    if file_info.get("has_error"):
        return False

    with open(file_path, 'r', encoding='utf-8') as f:
        content = f.read()

    # 检查是否已经有"暴露接口"
    if "暴露接口：" in content:
        return False

    # 提取现有的文件头注释
    docstring_match = re.search(r'^"""(.*?)"""', content, re.DOTALL)
    if not docstring_match:
        return False

    old_docstring = docstring_match.group(0)
    old_content = docstring_match.group(1).strip()

    # 跳过一些特殊文件
    if any(x in file_path for x in ['__pycache__', 'migrations', 'tests']):
        return False

    # 只保留文件职责描述
    lines = old_content.split('\n')
    file_desc = lines[0] if lines else "模块功能"

    # 构建新的文件头
    interface_list = []
    for func_name, func_sig in file_info.get("functions", []):
        interface_list.append(f"- {func_sig}：{func_name}功能")
    for cls_name in file_info.get("classes", []):
        interface_list.append(f"- {cls_name}：{cls_name}类")

    if not interface_list:
        # 没有接口的文件，不修改
        return False

    new_docstring = f'''"""
{file_desc}

暴露接口：
{chr(10).join(interface_list)}
"""'''

    new_content = content.replace(old_docstring, new_docstring, 1)

    if new_content != content:
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(new_content)
        return True

    return False


def simplify_function_comments(file_path: str) -> bool:
    """简化函数注释，删除 Args/Returns"""
    with open(file_path, 'r', encoding='utf-8') as f:
        content = f.read()

    modified = False
    new_content = content

    # 匹配函数文档字符串
    pattern = r'(\s+def\s+\w+\([^)]*\)(?:\s*->\s*[^:]+)?:\s*\n\s+)"""(.*?)"""'

    def replace_docstring(match):
        nonlocal modified
        indent = match.group(1)
        docstring = match.group(2).strip()

        # 检查是否包含 Args 或 Returns
        if "Args:" in docstring or "Returns:" in docstring or "Raises:" in docstring:
            # 提取第一行作为简洁描述
            lines = docstring.split('\n')
            first_line = lines[0].strip()

            # 跳过 Args/Returns/Raises 部分
            clean_lines = []
            skip_sections = False
            for line in lines:
                line = line.strip()
                if line in ("Args:", "Returns:", "Raises:", "Note:", "Example:", "Examples:"):
                    skip_sections = True
                    continue
                if not skip_sections and line and not line.startswith('-'):
                    clean_lines.append(line)
                if skip_sections and line and not line.startswith('-') and ':' not in line:
                    # 遇到新段落，停止跳过
                    if clean_lines:
                        break

            if clean_lines:
                new_desc = clean_lines[0]
            else:
                new_desc = first_line

            new_docstring = f'"""{new_desc}"""'
            modified = True
            return f'{indent}{new_docstring}'

        return match.group(0)

    new_content = re.sub(pattern, replace_docstring, content, flags=re.DOTALL)

    if modified:
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(new_content)
        return True

    return False


def main():
    """主函数"""
    src_dir = Path("d:/Jianguoyun/Agent/src")

    if not src_dir.exists():
        print(f"错误：目录不存在 {src_dir}")
        return

    python_files = list(src_dir.rglob("*.py"))
    print(f"找到 {len(python_files)} 个 Python 文件")

    modified_count = 0
    header_count = 0
    function_count = 0

    for file_path in python_files:
        str_path = str(file_path)

        # 跳过一些特殊目录
        skip_dirs = ['__pycache__', '.git', 'node_modules', 'migrations']
        if any(x in str_path for x in skip_dirs):
            continue

        try:
            # 优化文件头
            file_info = extract_file_info(str_path)
            if optimize_file_header(str_path, file_info):
                header_count += 1
                modified_count += 1
                print(f"[OK] 优化文件头: {str_path}")

            # 简化函数注释
            if simplify_function_comments(str_path):
                function_count += 1
                modified_count += 1
                print(f"[OK] 简化函数注释: {str_path}")

        except Exception as e:
            print(f"[ERROR] 处理失败 {str_path}: {e}")

    print("\n完成！")
    print(f"- 优化文件头: {header_count} 个文件")
    print(f"- 简化函数注释: {function_count} 个文件")
    print(f"- 总计修改: {modified_count} 处")


if __name__ == "__main__":
    main()
