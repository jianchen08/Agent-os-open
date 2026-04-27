"""StreamRepetitionMonitor 单元测试。"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from plugins.core.stream_repeat_monitor import StreamRepetitionMonitor


def test_no_repetition_returns_none():
    """正常不重复的内容不会触发 stop。"""
    chunks_received = []

    def original(chunk):
        chunks_received.append(chunk)

    monitor = StreamRepetitionMonitor(
        original,
        window=50,
        interval=30,
        similarity=0.9,
        trigger=2,
    )

    for i in range(20):
        result = monitor({
            "type": "text",
            "content": f"这是第{i}段完全不同的内容，包含唯一标识 ",
        })
        assert result is None, f"chunk {i} 不应触发 stop"

    assert len(chunks_received) == 20


def test_exact_repetition_triggers_stop():
    """完全相同的内容重复应触发 stop。"""
    monitor = StreamRepetitionMonitor(
        None,
        window=50,
        interval=30,
        similarity=0.9,
        trigger=2,
    )

    repeated = "A" * 30  # 30 个 A，超过 interval

    # 先填充 buffer 到超过 window*2
    for _ in range(4):
        monitor({"type": "text", "content": repeated})

    # 然后继续发，应该检测到重复
    result = None
    for _ in range(20):
        result = monitor({"type": "text", "content": repeated})
        if result == "stop":
            break

    assert result == "stop", "完全重复内容应该触发 stop"


def test_pattern_repetition_triggers_stop():
    """重复的模式内容触发 stop（模拟真实模型 bug）。"""
    monitor = StreamRepetitionMonitor(
        None,
        window=100,
        interval=50,
        similarity=0.9,
        trigger=3,
    )

    pattern = "The function returns the value of the variable x. "  # ~50 chars

    result = None
    for _ in range(50):
        result = monitor({"type": "text", "content": pattern})
        if result == "stop":
            break

    assert result == "stop", "重复模式内容应该触发 stop"


def test_forward_to_original_callback():
    """监控器应该将 chunk 转发给原始回调。"""
    received = []

    def original(chunk):
        received.append(chunk)

    monitor = StreamRepetitionMonitor(
        original,
        window=50,
        interval=30,
    )

    for i in range(5):
        monitor({"type": "text", "content": f"text {i}"})

    assert len(received) == 5
    assert received[0]["type"] == "text"
    assert received[3]["content"] == "text 3"


def test_non_text_chunks_ignored():
    """非 text 类型的 chunk 不参与重复检测。"""
    monitor = StreamRepetitionMonitor(
        None,
        window=50,
        interval=30,
        similarity=0.9,
        trigger=2,
    )

    # 大量重复的 thinking chunks 不触发
    for _ in range(100):
        result = monitor({"type": "thinking", "content": "same thinking"})
        assert result is None

    # text chunks 正常检测
    repeated = "B" * 60
    for _ in range(30):
        result = monitor({"type": "text", "content": repeated})
        if result == "stop":
            break

    assert result == "stop", "text 重复应该触发 stop"


def test_short_content_no_false_positive():
    """总长度不够时不应该误触发。"""
    monitor = StreamRepetitionMonitor(
        None,
        window=100,
        interval=50,
        similarity=0.9,
        trigger=3,
    )

    for i in range(5):
        result = monitor({"type": "text", "content": "hi"})
        assert result is None


def test_different_content_resets_counter():
    """不同的内容重置重复计数器。"""
    monitor = StreamRepetitionMonitor(
        None,
        window=30,
        interval=15,
        similarity=0.9,
        trigger=3,
    )

    repeated = "X" * 20

    # 发一轮重复
    for _ in range(5):
        result = monitor({"type": "text", "content": repeated})
        if result == "stop":
            break
    count_after_repeat = monitor._repeat_count

    # 插入不同的内容
    monitor({"type": "text", "content": "completely different and unique text!!!"})

    # repeat_count 应该被重置
    assert monitor._repeat_count == 0, (
        f"不同内容应该重置计数器，之前={count_after_repeat}"
    )


def test_configurable_trigger():
    """trigger 参数控制触发次数。"""
    # trigger=2
    monitor_fast = StreamRepetitionMonitor(
        None, window=30, interval=15, similarity=0.9, trigger=2,
    )
    # trigger=6
    monitor_slow = StreamRepetitionMonitor(
        None, window=30, interval=15, similarity=0.9, trigger=6,
    )

    repeated = "Z" * 20
    fast_stop = None
    slow_stop = None

    for i in range(200):
        r = monitor_fast({"type": "text", "content": repeated})
        if r == "stop" and fast_stop is None:
            fast_stop = i

        r = monitor_slow({"type": "text", "content": repeated})
        if r == "stop" and slow_stop is None:
            slow_stop = i

        if fast_stop is not None and slow_stop is not None:
            break

    assert fast_stop is not None, "trigger=2 应该触发"
    assert slow_stop is not None, "trigger=6 应该触发"
    assert slow_stop > fast_stop, (
        f"trigger=6 应该比 trigger=2 晚触发 "
        f"(fast={fast_stop}, slow={slow_stop})"
    )


if __name__ == "__main__":
    tests = [
        test_no_repetition_returns_none,
        test_exact_repetition_triggers_stop,
        test_pattern_repetition_triggers_stop,
        test_forward_to_original_callback,
        test_non_text_chunks_ignored,
        test_short_content_no_false_positive,
        test_different_content_resets_counter,
        test_configurable_trigger,
    ]

    for t in tests:
        t()
        print(f"PASS: {t.__name__}")

    print(f"\n全部 {len(tests)} 个测试通过!")
