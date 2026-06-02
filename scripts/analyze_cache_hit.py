"""分析 _validate_tool_call_pairing 对缓存命中率的影响。

关键发现：_validate_tool_call_pairing 每轮都在往消息序列中
插入 "Tool execution result unavailable." 消息，
这些额外插入的消息改变了前缀，导致缓存失效。
"""

import re
from dataclasses import dataclass


LOG_PATH = r"d:\myproject\container_08f57bc14532\logs\pipeline_81f98f451dc4.log"

RE_USAGE = re.compile(
    r"(\d{2}:\d{2}:\d{2}).*?"
    r"'input_tokens':\s*(\d+),\s*'output_tokens':\s*(\d+),\s*'total_tokens':\s*(\d+),\s*'cached_tokens':\s*(\d+)"
)

RE_ITER = re.compile(
    r"(\d{2}:\d{2}:\d{2}).*?iter=(\d+)\s+LLM returned"
)

RE_PATCHED = re.compile(
    r"(\d{2}:\d{2}:\d{2}).*?patched (\d+) missing tool results"
)

RE_SENDING = re.compile(
    r"(\d{2}:\d{2}:\d{2}).*?Sending\s+(\d+)\s+messages"
)

RE_REMINDER = re.compile(
    r"TaskReminder\[iter=(\d+)\].*?injecting reminder #(\d+)/\d+"
)

RE_NOTIFICATION = re.compile(
    r"\[Engine\] 迭代 (\d+) 开始时消费 (\d+) 条待处理通知"
)

RE_TOOL_SCHEMA = re.compile(
    r"\[tool_schema\] active_tool_ids=\[(.+?)\] \(count=(\d+)\)"
)

RE_SYSTEM_MSG = re.compile(
    r"\[prompt_build\] SystemMessage built \| content_len=(\d+) \| dynamic_vars=(\w+)"
)


@dataclass
class IterStats:
    """每轮迭代的统计信息。"""
    iter_num: int = 0
    input_tokens: int = 0
    cached_tokens: int = 0
    output_tokens: int = 0
    msg_count: int = 0
    patched_count: int = 0
    has_reminder: bool = False
    reminder_num: int = 0
    has_notification: bool = False
    notification_count: int = 0
    tool_count: int = 0
    tool_ids: list = None
    system_prompt_len: int = 0

    @property
    def cache_hit_rate(self) -> float:
        return self.cached_tokens / self.input_tokens if self.input_tokens else 0

    @property
    def uncached_tokens(self) -> int:
        return self.input_tokens - self.cached_tokens


def parse_log() -> list[IterStats]:
    """解析日志，提取每轮统计信息。"""
    usage_by_ts = {}
    iter_by_ts = {}
    patched_by_ts = {}
    msgs_by_ts = {}
    reminders = {}
    notifications = {}
    tools_by_ts = {}
    sp_by_ts = {}

    with open(LOG_PATH, "r", encoding="utf-8") as f:
        for line in f:
            ts_match = re.match(r"(\d{2}:\d{2}:\d{2})", line)
            if not ts_match:
                continue
            ts = ts_match.group(1)

            m = RE_USAGE.search(line)
            if m:
                usage_by_ts[ts] = {
                    "input_tokens": int(m.group(2)),
                    "output_tokens": int(m.group(3)),
                    "cached_tokens": int(m.group(5)),
                }

            m = RE_ITER.search(line)
            if m:
                iter_by_ts[ts] = int(m.group(2))

            m = RE_PATCHED.search(line)
            if m:
                patched_by_ts[ts] = int(m.group(2))

            m = RE_SENDING.search(line)
            if m:
                msgs_by_ts[ts] = int(m.group(2))

            m = RE_TOOL_SCHEMA.search(line)
            if m:
                tool_ids = [t.strip().strip("'\"") for t in m.group(1).split(",")]
                tools_by_ts[ts] = (int(m.group(2)), tool_ids)

            m = RE_SYSTEM_MSG.search(line)
            if m:
                sp_by_ts[ts] = int(m.group(1))

            m = RE_REMINDER.search(line)
            if m:
                reminders[int(m.group(1))] = int(m.group(2))

            m = RE_NOTIFICATION.search(line)
            if m:
                notifications[int(m.group(1))] = int(m.group(2))

    calls: dict[int, IterStats] = {}
    for ts, usage in usage_by_ts.items():
        it = iter_by_ts.get(ts)
        if it is None:
            continue
        s = IterStats(
            iter_num=it,
            input_tokens=usage["input_tokens"],
            output_tokens=usage["output_tokens"],
            cached_tokens=usage["cached_tokens"],
        )
        if ts in patched_by_ts:
            s.patched_count = patched_by_ts[ts]
        if ts in msgs_by_ts:
            s.msg_count = msgs_by_ts[ts]
        if ts in tools_by_ts:
            s.tool_count, s.tool_ids = tools_by_ts[ts]
        if ts in sp_by_ts:
            s.system_prompt_len = sp_by_ts[ts]
        if it in reminders:
            s.has_reminder = True
            s.reminder_num = reminders[it]
        if it in notifications:
            s.has_notification = True
            s.notification_count = notifications[it]
        calls[it] = s

    return [calls[k] for k in sorted(calls.keys())]


def main() -> None:
    """主函数。"""
    stats = parse_log()

    print("=" * 140)
    print(
        f"{'Iter':>4} | {'Input':>10} | {'Cached':>10} | {'Uncached':>10} | "
        f"{'Hit%':>6} | {'Msgs':>4} | {'Patched':>7} | {'SP Len':>6} | "
        f"{'Tools':>5} | {'Reminder':>8} | {'Notif':>5} | {'Uncached-Patched*50':>20}"
    )
    print("-" * 140)

    for s in stats:
        reminder_str = f"#{s.reminder_num}" if s.has_reminder else "-"
        notif_str = str(s.notification_count) if s.has_notification else "-"
        patched_estimate = s.patched_count * 50
        uncached_minus_patched = s.uncached_tokens - patched_estimate
        marker = " ⚠️" if s.cache_hit_rate < 0.5 else ""

        print(
            f"{s.iter_num:>4} | {s.input_tokens:>10,} | {s.cached_tokens:>10,} | "
            f"{s.uncached_tokens:>10,} | {s.cache_hit_rate:>5.1%} | {s.msg_count:>4} | "
            f"{s.patched_count:>7} | {s.system_prompt_len:>6} | {s.tool_count:>5} | "
            f"{reminder_str:>8} | {notif_str:>5} | {uncached_minus_patched:>18,}{marker}"
        )

    print("\n" + "=" * 100)
    print("关键分析：patched_count 变化与缓存命中率的关系")
    print("=" * 100)

    prev_patched = None
    for s in stats:
        if prev_patched is not None and s.patched_count != prev_patched:
            delta = s.patched_count - prev_patched
            print(f"  Iter {s.iter_num}: patched_count 从 {prev_patched} 变为 {s.patched_count} (+{delta}), 命中率={s.cache_hit_rate:.1%}")
        prev_patched = s.patched_count

    print("\n" + "=" * 100)
    print("验证：patched_count 不变时，缓存命中率是否稳定？")
    print("=" * 100)

    from itertools import groupby
    groups = []
    for k, g in groupby(stats, key=lambda s: s.patched_count):
        group = list(g)
        hit_rates = [s.cache_hit_rate for s in group]
        low_count = sum(1 for r in hit_rates if r < 0.5)
        high_count = sum(1 for r in hit_rates if r > 0.9)
        print(f"\n  patched_count={k}: {len(group)} 轮迭代")
        print(f"    命中率范围: {min(hit_rates):.1%} ~ {max(hit_rates):.1%}")
        print(f"    低命中率(<50%): {low_count} 轮, 高命中率(>90%): {high_count} 轮")
        for s in group:
            marker = " ⚠️" if s.cache_hit_rate < 0.5 else ""
            reminder_str = f"reminder #{s.reminder_num}" if s.has_reminder else ""
            notif_str = f"notif={s.notification_count}" if s.has_notification else ""
            extras = ", ".join(filter(None, [reminder_str, notif_str]))
            print(f"      Iter {s.iter_num}: hit={s.cache_hit_rate:.1%}, uncached={s.uncached_tokens:,}, msgs={s.msg_count} {extras}{marker}")

    print("\n" + "=" * 100)
    print("核心问题：patched 消息插入位置是否改变前缀？")
    print("=" * 100)
    print("""
_validate_tool_call_pairing 的逻辑：
1. 遍历 messages，找到 assistant 消息中的 tool_calls
2. 检查后续是否有对应的 tool result
3. 如果没有，插入 "Tool execution result unavailable." 作为占位

问题在于：这些 patched 消息是插入到消息序列的中间位置！
比如：
  原始: [system] [user] [assistant(tool_calls=[A,B,C])] [tool(A)] [tool(B)] [user]
  patched: [system] [user] [assistant(tool_calls=[A,B,C])] [tool(A)] [tool(B)] [tool(C, patched)] [user]
  
如果上一轮只有 A 和 B 的 tool result，这一轮 patching 了 C，
那么 C 的 tool result 是新插入的，改变了 [user] 之前的内容，
导致前缀不匹配！
""")

    print("\n" + "=" * 100)
    print("进一步验证：对比 patched_count 变化前后的 uncached_tokens")
    print("=" * 100)

    prev_s = None
    for s in stats:
        if prev_s is not None:
            input_delta = s.input_tokens - prev_s.input_tokens
            uncached_delta = s.uncached_tokens - prev_s.uncached_tokens
            cached_delta = s.cached_tokens - prev_s.cached_tokens
            patched_delta = s.patched_count - prev_s.patched_count

            if abs(cached_delta) > 50000:
                print(f"\n  Iter {prev_s.iter_num} → {s.iter_num}:")
                print(f"    input: {prev_s.input_tokens:,} → {s.input_tokens:,} (Δ={input_delta:+,})")
                print(f"    cached: {prev_s.cached_tokens:,} → {s.cached_tokens:,} (Δ={cached_delta:+,})")
                print(f"    uncached: {prev_s.uncached_tokens:,} → {s.uncached_tokens:,} (Δ={uncached_delta:+,})")
                print(f"    patched: {prev_s.patched_count} → {s.patched_count} (Δ={patched_delta:+d})")
                print(f"    命中率: {prev_s.cache_hit_rate:.1%} → {s.cache_hit_rate:.1%}")
        prev_s = s


if __name__ == "__main__":
    main()
