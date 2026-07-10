"""测试 fix_20260422_context_overflow 的两个修复点"""

# ===== 测试修复点1：工具输出截断 =====
MAX_TOOL_OUTPUT_LENGTH = 50000


def _truncate_output(output):
    """截断逻辑（从 executor.py 提取）"""
    if isinstance(output, str) and len(output) > MAX_TOOL_OUTPUT_LENGTH:
        truncated = output[:MAX_TOOL_OUTPUT_LENGTH]
        total_len = len(output)
        return truncated + (
            f"\n\n[输出已截断，共 {total_len} 字符，"
            f"仅显示前 {MAX_TOOL_OUTPUT_LENGTH} 字符]"
        )
    return output


def test_truncate_output():
    # 短输出不截断
    assert _truncate_output("hello") == "hello"

    # 长输出截断
    long_output = "A" * 60000
    result = _truncate_output(long_output)
    assert len(result) < 60000
    assert "[输出已截断" in result
    assert result.startswith("A" * 50000)

    # Dict 不变
    assert _truncate_output({"k": "v"}) == {"k": "v"}

    # None 不变
    assert _truncate_output(None) is None

    # 边界值：恰好等于阈值不截断
    boundary = "X" * 50000
    assert _truncate_output(boundary) == boundary

    # 边界值：超过阈值1个字符
    over = "X" * 50001
    result = _truncate_output(over)
    assert "[输出已截断" in result

    print("修复点1（工具输出截断）: 全部 6 个测试通过")


# ===== 测试修复点2：上下文溢出错误识别 =====
def _is_context_overflow(error_lower):
    return (
        "context window exceeds" in error_lower
        or "context_length_exceeded" in error_lower
        or "context length" in error_lower
        or ("max_tokens" in error_lower and "exceed" in error_lower)
        or ("token" in error_lower and "limit" in error_lower)
    )


def _is_llm_fixable(error_lower):
    """新的 is_llm_fixable 逻辑"""
    if _is_context_overflow(error_lower):
        return False
    return (
        "invalid function arguments" in error_lower
        or "invalid params" in error_lower
    )


CONTEXT_OVERFLOW_KEEP_RECENT = 6


def _truncate_context_messages(state):
    """截断逻辑（从 engine.py 提取）"""
    messages = state.get("messages", [])
    if not messages:
        return

    system_msgs = [m for m in messages if m.get("role") == "system"]
    other_msgs = [m for m in messages if m.get("role") != "system"]

    if len(other_msgs) <= CONTEXT_OVERFLOW_KEEP_RECENT:
        return

    kept = other_msgs[-CONTEXT_OVERFLOW_KEEP_RECENT:]
    state["messages"] = system_msgs + kept


def test_context_overflow_detection():
    # 上下文溢出场景
    assert _is_context_overflow("error: context window exceeds maximum")
    assert _is_context_overflow("context_length_exceeded")
    assert _is_context_overflow("context length exceeded the limit")
    assert _is_context_overflow("max_tokens exceed the limit")
    assert _is_context_overflow("token limit reached")

    # 普通参数错误不算溢出
    assert not _is_context_overflow("invalid params")
    assert not _is_context_overflow("invalid function arguments")
    assert not _is_context_overflow("timeout error")

    print("  - 上下文溢出检测: OK")


def test_is_llm_fixable_excludes_overflow():
    # 普通 invalid params -> 可修正
    assert _is_llm_fixable("invalid params: tool_call_id is required")

    # 普通 invalid function arguments -> 可修正
    assert _is_llm_fixable("invalid function arguments in tool call")

    # MiniMax 上下文溢出（含 invalid params） -> 不可修正
    assert not _is_llm_fixable(
        "invalid params: context window exceeds the maximum token limit"
    )

    # 纯上下文溢出 -> 不可修正
    assert not _is_llm_fixable("context_length_exceeded")
    assert not _is_llm_fixable("token limit exceeded")

    # 超时错误 -> 不可修正
    assert not _is_llm_fixable("timeout after 30s")

    print("  - is_llm_fixable 排除溢出: OK")


def test_truncate_context_messages():
    # 构造消息列表：1 个 system + 10 个 user/assistant
    messages = [{"role": "system", "content": "system prompt"}]
    for i in range(10):
        messages.append({"role": "user", "content": f"msg {i}"})

    state = {"messages": messages}

    # 截断前有 11 条消息
    assert len(state["messages"]) == 11

    _truncate_context_messages(state)

    # 截断后：1 system + 6 recent = 7 条
    assert len(state["messages"]) == 7
    assert state["messages"][0]["role"] == "system"
    assert state["messages"][1]["content"] == "msg 4"  # 保留 msg 4-9
    assert state["messages"][-1]["content"] == "msg 9"

    # 消息数不足时不截断
    short_state = {"messages": [{"role": "user", "content": "hi"}]}
    _truncate_context_messages(short_state)
    assert len(short_state["messages"]) == 1

    # 空消息列表不报错
    empty_state = {"messages": []}
    _truncate_context_messages(empty_state)
    assert len(empty_state["messages"]) == 0

    print("  - 消息截断: OK")


if __name__ == "__main__":
    test_truncate_output()

    print("修复点2（上下文溢出错误识别）:")
    test_context_overflow_detection()
    test_is_llm_fixable_excludes_overflow()
    test_truncate_context_messages()

    print("\n全部测试通过!")
