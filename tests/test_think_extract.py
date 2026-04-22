import re
import sys

sys.path.insert(0, "src")
from llm.adapter import _extract_thinking_from_content

passed = 0
failed = 0


def check(name, actual, expected):
    global passed, failed
    if actual == expected:
        passed += 1
        print(f"  PASS: {name}")
    else:
        failed += 1
        print(f"  FAIL: {name}")
        print(f"    expected: {expected!r}")
        print(f"    actual:   {actual!r}")


print("=== Test 1: MiniMax style (<think\\n...</think/>) ===")
t, c = _extract_thinking_from_content(
    "<think\n\n用户想测试。\n\n</think/>\n\n\n正文内容"
)
check("thinking not None", t is not None, True)
check("thinking content", t, "用户想测试。")
check("cleaned content", c, "正文内容")

print("\n=== Test 2: DeepSeek R1 actual data ===")
t, c = _extract_thinking_from_content(
    "<think\n\n完美！Agent 搜索也正常工作。\n\n现在可以总结。\n\n</think/>\n\n\n完美！测试成功！"
)
check("thinking not None", t is not None, True)
check("thinking content", t, "完美！Agent 搜索也正常工作。\n\n现在可以总结。")
check("cleaned content", c, "完美！测试成功！")

print("\n=== Test 3: Standard XML <think...</think...> ===")
t, c = _extract_thinking_from_content(
    "<think\n思考过程...\n</think >\n正文"
)
check("thinking not None", t is not None, True)
check("thinking content", t, "思考过程...")
check("cleaned content", c, "正文")

print("\n=== Test 4: With attrs <think type=...>...</think...> ===")
t, c = _extract_thinking_from_content(
    '<think type="reasoning">\n分析中...\n</think >\n结果'
)
check("thinking not None", t is not None, True)
check("thinking content", t, "分析中...")

print("\n=== Test 5: No think tags ===")
t, c = _extract_thinking_from_content("正常文本")
check("thinking is None", t, None)
check("content unchanged", c, "正常文本")

print("\n=== Test 6: None content ===")
t, c = _extract_thinking_from_content(None)
check("thinking is None", t, None)
check("content is None", c, None)

print("\n=== Test 7: Multiple think blocks ===")
t, c = _extract_thinking_from_content(
    "<think\n第一段\n</think/>\n中间\n<think\n第二段\n</think/>\n最终正文"
)
check("thinking merged", t, "第一段\n第二段")

print("\n=== Test 8: Empty think block ===")
t, c = _extract_thinking_from_content("<think\n\n</think/>\n\nOnly content")
check("thinking is None (empty)", t, None)
check("cleaned content", c, "Only content")

print("\n=== Test 9: Only thinking, no content after ===")
t, c = _extract_thinking_from_content("<think\n纯思考\n</think/>")
check("thinking not None", t is not None, True)
check("content is None (empty after strip)", c, None)

print("\n=== Test 10: Empty string ===")
t, c = _extract_thinking_from_content("")
check("thinking is None", t, None)
check("content is empty", c, "")

print("\n=== Test 11: </think/> end tag ===")
t, c = _extract_thinking_from_content("<think\n思考内容\n</think/>\n正文")
check("thinking not None", t is not None, True)
check("thinking content", t, "思考内容")
check("cleaned content", c, "正文")

print("\n=== Test 12: Normal content with HTML (no false positives) ===")
t, c = _extract_thinking_from_content("<div>HTML content</div>")
check("thinking is None", t, None)
check("content unchanged", c, "<div>HTML content</div>")

print("\n=== Test 13: Actual data from YAML record ===")
t, c = _extract_thinking_from_content(
    "<think\n\n用户要求搜索AI编程经验和工具链，工作目录为 research_results。这是一个调研任务，我需要：\n\n\n"
    "1. 使用 resource_search 搜索系统中的相关资源\n\n"
    "2. 使用 web_search 搜索网络上的相关资料\n\n"
    "3. 整理分析结果并输出调研报告\n\n\n"
    "让我先搜索系统资源，然后进行网络搜索。\n\n"
    "</think/>\n\n\n"
    "我将执行AI编程经验和工具链的调研任务。让我先搜索系统资源，然后进行网络搜索。\n\n    "
)
check("thinking not None", t is not None, True)
check("thinking contains key info", "AI编程经验和工具链" in t, True)
check("cleaned not None", c is not None, True)
check("cleaned does not contain think tags", "<think" in c, False)

print(f"\n{'='*50}")
print(f"Results: {passed} passed, {failed} failed")
sys.exit(0 if failed == 0 else 1)
