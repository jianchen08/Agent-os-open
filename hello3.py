"""Hello World 示例脚本（第三个变体）。

用于验证带工作空间、不带评估指标的代码任务流程。
"""


def hello() -> str:
    """返回经典问候语。

    Returns:
        str: 固定的 "Hello, World!" 字符串。
    """
    return "Hello, World!"


if __name__ == "__main__":
    print(hello())
