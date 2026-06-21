"""Hello World 示例脚本。

用于验证工作空间代码任务流程。
"""


def hello() -> str:
    """返回经典问候语。

    Returns:
        str: 固定的 "Hello, World!" 字符串。
    """
    return "Hello, World!"


if __name__ == "__main__":
    print(hello())