"""
推理提示模板

定义推理提示词的模板
"""

REASONING_PROMPT_TEMPLATE = """⚠️ [系统提示] 你尝试执行高风险操作但缺少推理过程。

在执行 {tool_name} 前，你必须完成三步推理：

🤔 步骤1：意图分析
📋 用户说: [原始需求]
🎯 真实意图: [你理解的目标]
⚠️ 潜在误解: [可能的错误理解]
✅ 确认理解: [用一句话确认]

🔍 步骤2：影响分析
📂 直接影响: [会影响哪些文件/功能]
🔗 依赖关系: [谁在使用]
⚠️ 风险评估: [可能破坏什么]

📝 步骤3：执行策略
🎯 操作类型: [删除/重构/修改]
具体步骤:
  1. [步骤1]
  2. [步骤2]
🛡️ 保护措施: [如何验证]

完成推理后，再次调用工具。
"""


def generate_reasoning_prompt(tool_name: str, inputs: dict = None) -> str:
    """
    生成推理提示

    Args:
        tool_name: 工具名称
        inputs: 工具输入参数（可选）

    Returns:
        推理提示词
    """
    return REASONING_PROMPT_TEMPLATE.format(
        tool_name=tool_name, inputs=str(inputs) if inputs else ""
    )
